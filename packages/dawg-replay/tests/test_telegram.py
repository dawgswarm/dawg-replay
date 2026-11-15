"""Telegram: клиент Bot API, привязка чата по коду, проверочное, отключение, последняя ошибка. Сеть только к фейку."""

import socket
import time

import pytest
from dawg_replay import telegram
from dawg_replay.app import App
from dawg_replay.config import load_config
from dawg_replay.errors import ReplayError
from dawg_replay.telegram import Bot, TelegramError
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app
from fake_telegram import CHAT_ID, TOKEN, FakeTelegram
from starlette.testclient import TestClient

CODE = "fixedcode123"
PORT = 54340
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def tg():
    with FakeTelegram() as fake:
        yield fake


@pytest.fixture
def tg_app(app, tg):
    app.set_config("telegram_api_url", tg.url)
    return app


@pytest.fixture
def fixed_code(monkeypatch):
    monkeypatch.setattr(telegram._secrets, "token_urlsafe", lambda n: CODE)


def free_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# --- клиент ---


def test_bot_methods(tg, tmp_path):
    bot = Bot(TOKEN, tg.url)
    assert bot.get_me()["username"] == "replay_test_bot"
    kb = {"inline_keyboard": [[{"text": "Да", "callback_data": "a:1"}]]}
    mid = bot.send_message(CHAT_ID, "привет", reply_markup=kb)
    assert isinstance(mid, int) and tg.sent()[-1]["reply_markup"] == kb and tg.sent()[-1]["text"] == "привет"
    f = tmp_path / "итог.csv"
    f.write_bytes(b"a,b\n1,2\n")
    assert isinstance(bot.send_document(CHAT_ID, f, caption="Готово"), int)
    doc = tg.documents[-1]
    assert doc["caption"] == "Готово" and doc["chat_id"] == str(CHAT_ID) and doc["document"]["content"] == b"a,b\n1,2\n"
    bot.edit_message_text(CHAT_ID, mid, "Ответ принят")
    assert tg.sent("editMessageText")[-1]["message_id"] == mid
    assert bot.delete_message(CHAT_ID, mid) is True
    bot.answer_callback_query("cb1", "Принято")
    assert tg.sent("answerCallbackQuery")[-1] == {"callback_query_id": "cb1", "text": "Принято"}
    tg.push_message("раз")
    tg.push_message("два")
    ups = bot.get_updates(timeout=0)
    assert [u["message"]["text"] for u in ups] == ["раз", "два"]
    assert bot.get_updates(ups[-1]["update_id"] + 1, timeout=0) == []
    tg.fail["deleteMessage"] = (400, "Bad Request: message to delete not found")
    assert bot.delete_message(CHAT_ID, 1) is False


def test_errors_explained_and_token_masked(tg):
    with pytest.raises(TelegramError) as ei:
        Bot("987654321:WRONGwrongWRONGwrongWRONG", tg.url).get_me()
    assert ei.value.code == 401 and "токен бота не подходит" in str(ei.value) and "WRONG" not in str(ei.value)
    tg.fail["sendMessage"] = (403, "Forbidden: bot was blocked by the user")
    with pytest.raises(TelegramError) as ei:
        Bot(TOKEN, tg.url).send_message(CHAT_ID, "x")
    assert ei.value.code == 403 and "заблокировал бота" in str(ei.value)
    # Сетевая ошибка httpx содержит адрес с токеном: наружу он не попадает.
    with pytest.raises(TelegramError) as ei:
        Bot(TOKEN, free_port_url(), timeout=2).get_me()
    assert ei.value.code is None and TOKEN not in str(ei.value) and TOKEN.split(":")[1] not in str(ei.value)


def test_mask_token():
    assert telegram.mask_token(f"GET https://api.telegram.org/bot{TOKEN}/getMe failed") == \
        "GET https://api.telegram.org/bot•••/getMe failed"
    assert telegram.mask_token("abc", token="abc") == "•••"
    assert telegram.mask_token("время 12:30, id 42") == "время 12:30, id 42"


# --- подключение ---


def test_not_connected(app):
    assert telegram.connection(app) is None
    st = telegram.status(app)
    assert st["connected"] is False and st["token_set"] is False and st["api_url"] == telegram.DEFAULT_API_URL
    with pytest.raises(ReplayError) as ei:
        telegram.send_test(app)
    assert ei.value.code == "telegram_not_connected"


def test_pairing_only_by_exact_code_in_private_chat(tg_app, tg, kr, home):
    app = tg_app
    pairing = telegram.start_pairing(app, f"  {TOKEN}\n")
    assert pairing.link == f"https://t.me/replay_test_bot?start={pairing.code}"
    assert pairing.command == f"/start {pairing.code}"
    assert app.secrets.get_telegram_token() == TOKEN
    assert telegram.connection(app) is None  # токен есть, чата ещё нет

    def script(fake):
        if fake.polls == 1:
            fake.push_message("/start", chat_id=1)
            fake.push_message("/start чужой-код", chat_id=2)
            fake.push_message(f"/start {pairing.code}", chat_id=3, chat_type="group")
            fake.push_message(f"/start@replay_test_bot {pairing.code}", chat_id=CHAT_ID, first_name="Иван")

    tg.on_get_updates = script
    got = telegram.wait_pairing(app, pairing, seconds=10, poll=1)
    assert got == {"chat_id": str(CHAT_ID), "chat_name": "Иван (@ivan)"}
    conn = telegram.connection(app)
    assert conn is not None and conn.chat_id == str(CHAT_ID) and conn.token == TOKEN and conn.api_url == tg.url
    assert [(m["chat_id"], m["text"]) for m in tg.sent()] == [(str(CHAT_ID), telegram.CONNECTED_TEXT)]
    assert tg.updates == []  # /start подтверждён: следующий опрос его не увидит
    cfg_text = (home / "config.toml").read_text(encoding="utf-8")
    assert TOKEN not in cfg_text and TOKEN.split(":")[1] not in cfg_text
    assert load_config(home / "config.toml").telegram_bot == "replay_test_bot"
    assert telegram.status(app)["connected"] is True


def test_pairing_timeout_and_bad_token(tg_app, tg):
    with pytest.raises(ReplayError) as ei:
        telegram.start_pairing(tg_app, "111111111:notTheRightTokenAtAll_xx")
    assert ei.value.code == "telegram_error" and "notTheRight" not in ei.value.message
    assert tg_app.secrets.get_telegram_token() is None  # неверный токен не сохраняется
    pairing = telegram.start_pairing(tg_app, TOKEN)
    start = time.monotonic()
    assert telegram.wait_pairing(tg_app, pairing, seconds=1, poll=1) is None
    assert time.monotonic() - start < 6
    assert telegram.connection(tg_app) is None


def test_test_message_error_and_disconnect(tg_app, tg, fixed_code):
    pairing = telegram.start_pairing(tg_app, TOKEN)
    tg.push_message(f"/start {CODE}")
    assert telegram.wait_pairing(tg_app, pairing, seconds=5, poll=1)
    telegram.send_test(tg_app)
    assert tg.sent()[-1]["text"] == telegram.TEST_TEXT
    assert telegram.last_error(tg_app) is None

    tg.fail["sendMessage"] = (403, "Forbidden: bot was blocked by the user")
    with pytest.raises(ReplayError) as ei:
        telegram.send_test(tg_app)
    assert "заблокировал бота" in ei.value.message
    err = telegram.last_error(tg_app)
    assert err and "заблокировал бота" in err["text"] and err["at"]
    del tg.fail["sendMessage"]
    telegram.send_test(tg_app)
    assert telegram.last_error(tg_app) is None  # удачная проверка снимает старую ошибку

    telegram.record_error(tg_app, f"сеть: https://x/bot{TOKEN}/sendMessage")
    assert TOKEN not in telegram.last_error(tg_app)["text"]

    telegram.disconnect(tg_app)
    assert tg_app.secrets.get_telegram_token() is None and telegram.connection(tg_app) is None
    st = telegram.status(tg_app)
    assert st["connected"] is False and st["bot"] is None and st["last_error"] is None


def test_api_url_validated(app):
    with pytest.raises(ReplayError):
        app.set_config("telegram_api_url", "api.telegram.org")
    assert app.set_config("telegram_api_url", "https://tg.example.org/").telegram_api_url == "https://tg.example.org/"


# --- CLI ---


def test_cli_connect_status_test_disconnect(cli, home, kr, tg, fixed_code, capsys):
    with App(home, keyring_backend=kr) as a:
        a.set_config("telegram_api_url", tg.url)
    tg.on_get_updates = lambda fake: fake.polls == 1 and fake.push_message(f"/start {CODE}")
    code, data = cli("telegram", "connect", "--wait", "10", stdin=TOKEN + "\n")
    assert code == 0, data
    assert data["telegram"]["connected"] is True and data["telegram"]["chat_name"] == "Иван (@ivan)"
    assert TOKEN not in str(data)

    code, data = cli("telegram", "status")
    assert code == 0 and data["telegram"]["bot"] == "replay_test_bot"
    code, data = cli("telegram", "test")
    assert code == 0 and data["sent"] is True

    tg.fail["sendMessage"] = (401, "Unauthorized")
    code, data = cli("telegram", "test")
    assert code != 0 and "токен бота не подходит" in data["error"]["message"]
    code, data = cli("telegram", "status")
    assert "токен бота не подходит" in data["telegram"]["last_error"]["text"]

    code, data = cli("telegram", "disconnect")
    assert code == 0 and data["telegram"]["connected"] is False
    assert kr.get_password("dawg-replay", "telegram-bot-token") is None


def test_cli_connect_timeout_keeps_token(cli, home, kr, tg):
    with App(home, keyring_backend=kr) as a:
        a.set_config("telegram_api_url", tg.url)
    code, data = cli("telegram", "connect", "--wait", "1", stdin=TOKEN + "\n")
    assert code != 0 and data["error"]["code"] == "telegram_timeout" and "--same-token" in data["error"]["message"]
    tg.on_get_updates = lambda fake: fake.polls == 2 and fake.push_message("/start nope")
    code, data = cli("telegram", "connect", "--same-token", "--wait", "1")
    assert code != 0 and data["error"]["code"] == "telegram_timeout"


# --- Настройки ---


@pytest.fixture
def web(home, kr, tg):
    with App(home, keyring_backend=kr) as a:
        a.set_config("telegram_api_url", tg.url)
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": secrets_.csrf}


def test_settings_block_pairing_flow(web, tg, fixed_code):
    c, h = web
    page = c.get("/settings").text
    assert "<h2 id=\"telegram-title\">Telegram</h2>" in page and "@BotFather" in page and 'name="token"' in page

    r = c.post("/settings/telegram/connect", data={"token": "1:bad"}, headers=h)
    assert "токен бота не подходит" in r.text

    r = c.post("/settings/telegram/connect", data={"token": TOKEN}, headers=h)
    assert f'href="https://t.me/replay_test_bot?start={CODE}"' in r.text and f"/start {CODE}" in r.text
    assert 'hx-trigger="every 2s"' in r.text and TOKEN not in r.text
    tg.push_message(f"/start {CODE}", first_name="Мария", username=None)
    for _ in range(100):
        r = c.get("/settings/telegram")
        if "every 2s" not in r.text:
            break
        time.sleep(0.05)
    assert "Подключено: бот пишет в чат «Мария»" in r.text
    assert "Отправить проверочное" in r.text and "every 2s" not in r.text
    assert "Подключено: бот пишет" not in c.get("/settings/telegram").text  # итог показан один раз

    r = c.post("/settings/telegram/test", headers=h)
    assert "Проверочное сообщение отправлено" in r.text
    tg.fail["sendMessage"] = (403, "Forbidden: bot was blocked by the user")
    r = c.post("/settings/telegram/test", headers=h)
    assert "заблокировал бота" in r.text and "Последняя ошибка отправки" in r.text

    r = c.post("/settings/telegram/disconnect", headers=h)
    assert "Telegram отключён" in r.text and 'name="token"' in r.text
    assert TOKEN not in c.get("/settings").text


def test_settings_pairing_cancel_and_relink(web, tg, fixed_code):
    c, h = web
    r = c.post("/settings/telegram/connect", data={"token": TOKEN}, headers=h)
    assert "every 2s" in r.text
    r = c.post("/settings/telegram/cancel", headers=h)
    assert "every 2s" not in r.text and "чат ещё не привязан" in r.text and "Привязать чат" in r.text
    r = c.post("/settings/telegram/connect", headers=h)  # сохранённый токен, без вставки
    assert "every 2s" in r.text and f"/start {CODE}" in r.text
    tg.push_message(f"/start {CODE}")
    for _ in range(100):
        r = c.get("/settings/telegram")
        if "every 2s" not in r.text:
            break
        time.sleep(0.05)
    assert "Подключено: бот пишет в чат «Иван (@ivan)»" in r.text
