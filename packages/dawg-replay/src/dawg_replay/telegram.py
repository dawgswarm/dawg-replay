"""Telegram: личный бот пользователя, привязка чата по одноразовому коду, последняя ошибка отправки.

Токен бота лежит в хранилище паролей (`SecretStore`), в config.toml только чат, имя бота и адрес
Bot API. Токен входит в адрес каждого запроса (`/bot<токен>/<метод>`), поэтому всё, что уходит
наружу из этого модуля (ошибки, журнал, «последняя ошибка»), проходит через `mask_token`.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay import tg_updates
from dawg_replay.errors import ReplayError

if TYPE_CHECKING:
    from dawg_replay.app import App

DEFAULT_API_URL = "https://api.telegram.org"
TIMEOUT = 10.0  # обычный запрос к Bot API
POLL_EXTRA = 10.0  # запас сверх long polling getUpdates
PAIR_SECONDS = 300  # сколько ждать /start <код>
CONNECTED_TEXT = "Подключено к dawg-replay. Сюда будут приходить сообщения о запусках."
TEST_TEXT = "Проверка связи: dawg-replay на месте, сообщения о запусках будут приходить сюда."
ERROR_FILE = "telegram-error.json"

# Токен бота: `<id>:<секрет>`. Ищется и без знания конкретного токена (адрес в ошибке httpx).
_TOKEN_RE = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}")
MASK = "•••"


class TelegramError(Exception):
    """Ошибка Bot API или сети. `code`: код Telegram (401, 403, 409...) или None для сети."""

    def __init__(self, code: int | None, description: str):
        self.code = code
        self.description = description
        super().__init__(description)

    def __str__(self) -> str:
        return self.description


def mask_token(text: str, token: str | None = None) -> str:
    """Скрывает токен бота: известный целиком и всё, что похоже на токен."""
    if not text:
        return text
    if token:
        text = text.replace(token, MASK)
    return _TOKEN_RE.sub(MASK, text)


def _explain(code: int | None, description: str) -> str:
    """Понятный текст для частых ошибок Telegram, исходное описание в скобках."""
    hints = {
        401: "токен бота не подходит: проверьте его у @BotFather",
        403: "бот не может написать в чат: пользователь заблокировал бота или не нажал Start",
        404: "токен бота не подходит: проверьте его у @BotFather",
        409: "обновления бота уже забирает другая программа или вебхук: отключите их или создайте отдельного бота",
        429: "Telegram просит писать реже, следующая попытка позже",
    }
    hint = hints.get(code or 0)
    return f"{hint} ({description})" if hint and description else hint or description


class Bot:
    """Тонкий клиент Bot API на httpx. Системный прокси учитывается, как у остальных запросов приложения."""

    def __init__(self, token: str, api_url: str = DEFAULT_API_URL, timeout: float = TIMEOUT):
        self.token = token
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, params: dict[str, Any] | None = None, *, files: dict | None = None,
              timeout: float | None = None) -> Any:
        import httpx

        url = f"{self.api_url}/bot{self.token}/{method}"
        try:
            if files:
                resp = httpx.post(url, data={k: str(v) for k, v in (params or {}).items()}, files=files,
                                  timeout=timeout or self.timeout)
            else:
                resp = httpx.post(url, json=params or {}, timeout=timeout or self.timeout)
        except httpx.HTTPError as e:
            raise TelegramError(None, mask_token(f"Telegram недоступен: {type(e).__name__}: {e}", self.token)) from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise TelegramError(resp.status_code, f"Telegram ответил HTTP {resp.status_code} без JSON")
        if not body.get("ok"):
            code = body.get("error_code") or resp.status_code
            desc = mask_token(str(body.get("description") or f"HTTP {resp.status_code}"), self.token)
            raise TelegramError(code, _explain(code, desc))
        return body.get("result")

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def send_message(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> int:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return int(self._call("sendMessage", params)["message_id"])

    def send_document(self, chat_id: int | str, path: str | Path, caption: str | None = None) -> int:
        p = Path(path)
        params: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        with open(p, "rb") as f:
            # Файл до 50 МБ может идти долго: таймаут больше обычного.
            result = self._call("sendDocument", params, files={"document": (p.name, f)}, timeout=max(self.timeout, 120.0))
        return int(result["message_id"])

    def edit_message_text(self, chat_id: int | str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text,
                                  "disable_web_page_preview": True}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        self._call("editMessageText", params)

    def delete_message(self, chat_id: int | str, message_id: int) -> bool:
        try:
            return bool(self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))
        except TelegramError as e:
            if e.code == 400:  # сообщение уже удалено или старше 48 часов
                return False
            raise

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        params: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        self._call("answerCallbackQuery", params)

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        """Long polling: ждёт до `timeout` секунд. Подтверждает все обновления с id меньше `offset`."""
        params: dict[str, Any] = {"timeout": int(timeout), "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return list(self._call("getUpdates", params, timeout=timeout + POLL_EXTRA) or [])


# --- подключение -----------------------------------------------------------------


@dataclass(frozen=True)
class Connection:
    token: str
    chat_id: str
    api_url: str
    chat_name: str = ""
    bot_name: str = ""

    def bot(self) -> Bot:
        return Bot(self.token, self.api_url)


def _api_url(app: App) -> str:
    return app.config.telegram_api_url or DEFAULT_API_URL


def connection(app: App) -> Connection | None:
    """Подключение, если есть и токен, и привязанный чат. Хранилище паролей недоступно: None."""
    cfg = app.config
    if not cfg.telegram_chat_id:
        return None
    try:
        token = app.secrets.get_telegram_token()
    except ReplayError:
        return None
    if not token:
        return None
    return Connection(token=token, chat_id=cfg.telegram_chat_id, api_url=_api_url(app),
                      chat_name=cfg.telegram_chat_name, bot_name=cfg.telegram_bot)


def status(app: App) -> dict[str, Any]:
    """Состояние для Настроек и `telegram status`. Сеть не трогает, токен наружу не отдаёт."""
    cfg = app.config
    try:
        has_token = bool(app.secrets.get_telegram_token())
        keyring_error = None
    except ReplayError as e:
        has_token, keyring_error = False, e.message
    return {
        "connected": has_token and bool(cfg.telegram_chat_id),
        "token_set": has_token,
        "bot": cfg.telegram_bot or None,
        "chat_name": cfg.telegram_chat_name or None,
        "api_url": _api_url(app),
        "api_url_custom": _api_url(app) != DEFAULT_API_URL,
        "last_error": last_error(app),
        "keyring_error": keyring_error,
    }


def _save(app: App, **values: str) -> None:
    from dawg_replay.config import save_config

    cfg = app.config
    for k, v in values.items():
        setattr(cfg, k, v)
    save_config(app.paths.config, cfg)


@dataclass(frozen=True)
class Pairing:
    """Начатая привязка: ссылка и команда для пользователя, код для ожидания."""

    code: str
    bot: str  # username бота без @
    bot_title: str

    @property
    def link(self) -> str:
        return f"https://t.me/{self.bot}?start={self.code}"

    @property
    def command(self) -> str:
        return f"/start {self.code}"

    def to_dict(self) -> dict[str, Any]:
        return {"bot": self.bot, "bot_title": self.bot_title, "link": self.link, "command": self.command}


def start_pairing(app: App, token: str | None = None, command: str | None = None) -> Pairing:
    """Проверяет токен (getMe), сохраняет его и сбрасывает прежний чат. Возвращает код привязки.

    Без `token` берётся уже сохранённый: привязать чат заново, не вставляя токен.
    `command`: команда для подсказки про вставку в терминал (CLI).
    """
    from dawg_replay.secrets import clean_secret

    if token is None:
        token = app.secrets.get_telegram_token()
        if not token:
            raise ReplayError("telegram_not_connected", "токен бота не задан")
    else:
        token = clean_secret(token, "токен бота", command=command)
    try:
        me = Bot(token, _api_url(app)).get_me()
    except TelegramError as e:
        raise ReplayError("telegram_error", mask_token(str(e), token)) from None
    username = (me or {}).get("username")
    if not username:
        raise ReplayError("telegram_error", "Telegram не вернул имя бота: проверьте токен у @BotFather")
    app.secrets.set_telegram_token(token)
    _save(app, telegram_chat_id="", telegram_chat_name="", telegram_bot=username)
    clear_error(app)
    code = _secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")  # для ?start= нужны буквы и цифры
    return Pairing(code=code, bot=username, bot_title=str(me.get("first_name") or username))


class PairingJob:
    """Ожидание `/start <код>` в фоновом потоке, для экрана Настроек.

    Состояние: `waiting`, `done`, `expired`, `cancelled`, `error`. У потока своё ядро (`open_app`):
    подключение SQLite не переходит между потоками.
    """

    def __init__(self, open_app: Callable[[], App], pairing: Pairing, seconds: float = PAIR_SECONDS, poll: int = 5):
        import threading

        self.pairing = pairing
        self.state = "waiting"
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.until = datetime.now(timezone.utc).timestamp() + seconds
        self._stop = threading.Event()
        self._open_app = open_app
        self._seconds = seconds
        self._poll = poll
        self.thread = threading.Thread(target=self._run, name="telegram-pairing", daemon=True)

    def start(self) -> PairingJob:
        self.thread.start()
        return self

    def cancel(self, wait: float = 0) -> None:
        if self.state == "waiting":
            self.state = "cancelled"
        self._stop.set()
        if wait:
            self.thread.join(wait)

    def _run(self) -> None:
        try:
            with self._open_app() as app:
                got = wait_pairing(app, self.pairing, seconds=self._seconds, poll=self._poll, stop=self._stop.is_set)
        except ReplayError as e:
            self.error, self.state = e.message, "error"
            return
        except Exception as e:  # noqa: BLE001  поток не должен умирать молча: экран покажет причину
            self.error, self.state = mask_token(f"{type(e).__name__}: {e}"), "error"
            return
        if got is not None:
            self.result, self.state = got, "done"
        elif self.state == "waiting":
            self.state = "expired"


def _start_code(message: dict[str, Any]) -> str | None:
    """Код из `/start <код>` или `/start@бот <код>`; иначе None."""
    text = (message.get("text") or "").strip()
    parts = text.split()
    if len(parts) != 2 or not (parts[0] == "/start" or parts[0].startswith("/start@")):
        return None
    return parts[1]


def _chat_name(message: dict[str, Any]) -> str:
    who = message.get("from") or message.get("chat") or {}
    name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x)
    if who.get("username"):
        name = f"{name} (@{who['username']})" if name else f"@{who['username']}"
    return name or str((message.get("chat") or {}).get("id", ""))


def wait_pairing(app: App, pairing: Pairing, *, seconds: float = PAIR_SECONDS, poll: int = 25,
                 stop: Callable[[], bool] = lambda: False) -> dict[str, Any] | None:
    """Ждёт `/start <код>` в личном чате с ботом. Дождался: запоминает чат, бот отвечает «Подключено».

    Возвращает {"chat_id", "chat_name"} или None, если время вышло или `stop()` вернул True.
    Сообщения с другим кодом, без кода и из групп не привязывают. Ошибка Telegram: ReplayError.
    """
    token = app.secrets.get_telegram_token()
    if not token:
        raise ReplayError("telegram_not_connected", "токен бота не задан")
    bot = Bot(token, _api_url(app))
    deadline = time.monotonic() + seconds
    # Один getUpdates на установку: пока ответа ждут вопросы скриптов, обновления забирает
    # их опрашивающий (tg_asks), привязка ждёт блокировку. Ответ на вопрос, пришедший во
    # время привязки, привязка не обрабатывает (см. dawg_replay.tg_updates).
    lock = tg_updates.UpdatesLock(app)
    try:
        while not stop():
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            if not lock.acquire(0):
                time.sleep(min(1.0, left))
                continue
            offset = tg_updates.load_offset(app)
            updates = bot.get_updates(offset, timeout=max(0, min(poll, int(left))))
            if stop():
                # Отменённая привязка не трогает полученное: его увидит новая привязка
                # или опрос ответов (offset не сдвигается).
                return None
            for u in updates:
                offset = max(offset or 0, int(u.get("update_id", 0)) + 1)
                tg_updates.save_offset(app, offset)
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                if chat.get("type") != "private" or not _secrets.compare_digest((_start_code(msg) or "").encode(), pairing.code.encode()):
                    continue
                chat_id, name = str(chat["id"]), _chat_name(msg)
                _save(app, telegram_chat_id=chat_id, telegram_chat_name=name)
                bot.get_updates(offset, timeout=0)  # подтвердить: следующий опрос не увидит /start снова
                try:
                    bot.send_message(chat_id, CONNECTED_TEXT)
                except TelegramError as e:
                    record_error(app, f"ответ «Подключено» не отправлен: {e}")
                return {"chat_id": chat_id, "chat_name": name}
    except TelegramError as e:
        raise ReplayError("telegram_error", mask_token(str(e), token)) from None
    finally:
        lock.release()
    return None


def send_test(app: App) -> int:
    """Проверочное сообщение. Ошибка записывается как последняя и уходит наружу ReplayError."""
    conn = connection(app)
    if conn is None:
        raise ReplayError("telegram_not_connected", "Telegram не подключён: dawg-replay telegram connect или Настройки")
    try:
        mid = conn.bot().send_message(conn.chat_id, TEST_TEXT)
    except TelegramError as e:
        record_error(app, str(e))
        raise ReplayError("telegram_error", mask_token(str(e), conn.token)) from None
    clear_error(app)
    return mid


def disconnect(app: App) -> None:
    """Стирает токен из хранилища паролей, чат и имя бота из настроек, последнюю ошибку."""
    app.secrets.delete_telegram_token()
    _save(app, telegram_chat_id="", telegram_chat_name="", telegram_bot="")
    clear_error(app)


# --- последняя ошибка ------------------------------------------------------------


def _error_path(app: App) -> Path:
    return app.paths.home / ERROR_FILE


def record_error(app: App, text: str) -> None:
    """Запоминает последнюю ошибку отправки для блока Telegram в Настройках. Сама не падает."""
    data = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "text": mask_token(str(text))[:500]}
    path = _error_path(app)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def last_error(app: App) -> dict[str, str] | None:
    """{"at": ISO-время UTC, "text": ...} или None."""
    try:
        data = json.loads(_error_path(app).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("text"):
        return None
    return {"at": str(data.get("at", "")), "text": str(data["text"])}


def clear_error(app: App) -> None:
    try:
        _error_path(app).unlink()
    except OSError:
        pass
