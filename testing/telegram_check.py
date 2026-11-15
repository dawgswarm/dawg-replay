"""Проверка уведомлений в Telegram по плану docs/acceptance/m5-telegram.md на фейковом Bot API.

Запуск из корня репозитория (macOS и Linux: регулярный запуск выполняется через /bin/sh):

    uv run python testing/telegram_check.py            # все сценарии
    uv run python testing/telegram_check.py P,R,UI     # выборочно: P R V C O N UI D

Всё идёт во временном каталоге (путь печатается в конце): свой каталог данных, keyring в файле
(testing/filekeyring.py) и фейковый crontab, системные хранилище паролей и crontab не трогаются.
Сеть только к фейку (testing/fake_telegram.py), ключ DAWG не нужен: демо examples/telegram-demo
без браузера. Команды CLI запускаются отдельными процессами, экраны через тестовый клиент Starlette.
Все сообщения, кнопки и ответы бота пишутся в messages.log во временном каталоге: по нему удобно
сверять тексты в плане. Сценарий P (подключение) нужен перед остальными, D (отключение) последним.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTING = ROOT / "testing"
PY = sys.executable
DR = shutil.which("dawg-replay", path=str(Path(sys.executable).parent)) or "dawg-replay"

sys.path.insert(0, str(TESTING))

from dawg_replay.app import App  # noqa: E402
from fake_telegram import CHAT_ID, TOKEN, FakeTelegram  # noqa: E402
from filekeyring import FileKeyring  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="telegram-check-"))
HOME = WORK / "home"
LOG = WORK / "messages.log"
os.environ.update({
    "DAWG_REPLAY_HOME": str(HOME),
    "PYTHON_KEYRING_BACKEND": "filekeyring.FileKeyring",
    "FILEKEYRING_PATH": str(WORK / "keyring.json"),
    "PYTHONPATH": os.pathsep.join([str(TESTING), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep),
    "DAWG_REPLAY_SCRIPT_COMMAND": json.dumps([PY]),
    "DAWG_REPLAY_SCHEDULER": "cron",
    "DAWG_REPLAY_CRONTAB": json.dumps([PY, str(ROOT / "packages/dawg-replay/tests/fakes/fake_crontab.py")]),
    "FAKE_CRONTAB_FILE": str(WORK / "crontab"),
})
tg = FakeTelegram()
KR = FileKeyring()
RESULTS: list[tuple[str, bool, str]] = []


def app() -> App:
    return App(HOME, keyring_backend=KR)


def cli(*args: str, stdin: str | None = None, check: bool = False) -> tuple[int, dict]:
    p = subprocess.run([DR, "--json", *args], input=stdin, capture_output=True, text=True, timeout=600)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        data = {"raw": p.stdout, "stderr": p.stderr}
    if check and p.returncode != 0:
        raise AssertionError(f"{args}: {p.returncode} {data} {p.stderr}")
    return p.returncode, data


def bg(*args: str) -> subprocess.Popen:
    return subprocess.Popen([DR, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def mark() -> int:
    return len(tg.calls)


def sent(since: int, method: str = "sendMessage") -> list[dict]:
    return [p for m, p in tg.calls[since:] if m == method]


def texts(since: int) -> list[str]:
    return [p["text"] for p in sent(since)]


def wait_for(cond, timeout: float = 30, step: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


def run_rec(run_id: str):
    with app() as a:
        return a.runs.get(run_id)


def wait_run(run_id: str, timeout: float = 120):
    return wait_for(lambda: (lambda r: r if not r.running else None)(run_rec(run_id)), timeout, 0.3)


def start(*args: str) -> str:
    code, data = cli("run", "telegram-demo", *args)
    assert code == 0, data
    return data["run"]["id"]


def check(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, bool(ok), note))
    print(("OK  " if ok else "BAD ") + name + (f": {note}" if note else ""), flush=True)


def log_msgs(title: str, since: int) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== {title}\n")
        for m, p in tg.calls[since:]:
            if m in ("sendMessage", "editMessageText"):
                f.write(f"--- {m}\n{p['text']}\n")
                if p.get("reply_markup"):
                    f.write(f"[кнопки] {json.dumps(p['reply_markup'], ensure_ascii=False)}\n")
            elif m != "getMe":
                f.write(f"--- {m} {json.dumps({k: v for k, v in p.items() if k != 'document'}, ensure_ascii=False)}\n")


def push_cb(message_id: int, data: str, cb_id: str = "cb") -> None:
    tg.push({"callback_query": {"id": cb_id, "from": {"id": CHAT_ID}, "data": data,
                                "message": {"message_id": message_id, "chat": {"id": CHAT_ID, "type": "private"}}}})


def push_reply(text: str, reply_to: int | None) -> int:
    mid = tg._next_message_id()
    msg = {"message_id": mid, "date": 0, "text": text, "chat": {"id": CHAT_ID, "type": "private"},
           "from": {"id": CHAT_ID, "first_name": "Иван"}}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    tg.push({"message": msg})
    return mid


def link_mids() -> dict[str, int]:
    from dawg_replay import tg_asks
    return {f"{x.run_id}:{x.ask_id}": x.message_id for x in tg_asks.load_links(HOME)}


def question_mid(run_id: str, timeout: float = 30) -> int | None:
    def find():
        for k, mid in link_mids().items():
            if k.startswith(run_id + ":") and mid is not None:
                return mid
        return None
    return wait_for(find, timeout)


# --- П -------------------------------------------------------------------------


def connect(token: str = TOKEN, extra: tuple[str, ...] = (), before=None, wait: int = 20) -> tuple[int, dict, str]:
    p = subprocess.Popen([DR, "--json", "telegram", "connect", "--wait", str(wait), *extra], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if "--same-token" not in extra:
        p.stdin.write(token + "\n")
    p.stdin.close()
    err = ""
    code = None
    while True:
        line = p.stderr.readline()
        if not line:
            break
        err += line
        m = re.search(r"/start (\S+)", line)
        if m and code is None:
            code = m.group(1)
            if before:
                before(code)
    out = p.stdout.read()
    p.wait()
    try:
        return p.returncode, json.loads(out), err
    except ValueError:
        return p.returncode, {"raw": out}, err


def s_P() -> None:
    with app() as a:
        a.set_config("telegram_api_url", tg.url)
    # П1: неверный токен, верный, привязка
    code, data, err = connect("111111111:wrongwrongwrongwrongwrong", wait=5)
    check("П1 неверный токен не принят", code != 0 and "токен бота не подходит" in data.get("error", {}).get("message", ""),
          data.get("error", {}).get("message", ""))
    m0 = mark()
    code, data, err = connect(before=lambda c: tg.push_message(f"/start {c}"))
    check("П1 подключение CLI", code == 0 and data["telegram"]["connected"], data.get("telegram", {}).get("chat_name", ""))
    check("П1 ответ «Подключено»", any(t.startswith("Подключено к dawg-replay") for t in texts(m0)), " | ".join(texts(m0)))
    check("П1 подсказка в терминале", "Откройте ссылку" in err and "t.me/replay_test_bot?start=" in err, err.splitlines()[0] if err else "")
    log_msgs("П1", m0)

    # П2: /start без кода и с чужим кодом, группа: не привязывают
    def wrong(c):
        tg.push_message("/start", chat_id=1)
        tg.push_message("/start 123", chat_id=2)
        tg.push_message(f"/start {c}", chat_id=3, chat_type="group")
    code, data, err = connect(before=wrong, wait=3)
    check("П2 чужие /start не привязали", code != 0 and data["error"]["code"] == "telegram_timeout", data.get("error", {}).get("message", ""))
    code, data, err = connect(extra=("--same-token",), before=lambda c: tg.push_message(f"/start {c}"))
    check("П2 привязка после неудачи", code == 0 and data["telegram"]["connected"])

    # П3: тот же токен, таймаут: чат прежний; другой бот: чат сброшен сразу
    code, data, err = connect(extra=("--same-token",), wait=2)
    m0 = mark()
    c2, d2 = cli("telegram", "test")
    check("П3 тем же ботом без /start чат не сброшен", code != 0 and c2 == 0 and sent(m0)[-1]["chat_id"] == str(CHAT_ID),
          data.get("error", {}).get("message", ""))
    other = "555555555:BBH-other_bot_token_0123456789abc"
    tg.token, tg.username = other, "other_bot"
    p = subprocess.Popen([DR, "--json", "telegram", "connect", "--wait", "30"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p.stdin.write(other + "\n")
    p.stdin.close()
    p.stdin = None
    wait_for(lambda: KR.get_password("dawg-replay", "telegram-bot-token") == other, 10)
    time.sleep(0.5)
    p.send_signal(signal.SIGINT)
    out, err = p.communicate(timeout=20)
    _, st = cli("telegram", "status")
    check("П3 другой бот сбросил чат сразу", st["telegram"]["connected"] is False and st["telegram"]["bot"] == "other_bot",
          f"Ctrl+C: {err.strip().splitlines()[-1] if err.strip() else out}")
    tg.token, tg.username = TOKEN, "replay_test_bot"
    code, data, err = connect(before=lambda c: tg.push_message(f"/start {c}"))
    check("П3 снова первый бот", code == 0 and data["telegram"]["bot"] == "replay_test_bot")

    # П4: проверочное, блокировка, разблокировка
    m0 = mark()
    c, d = cli("telegram", "test")
    check("П4 проверочное CLI", c == 0 and texts(m0) and texts(m0)[-1].startswith("Проверка связи"))
    tg.fail["sendMessage"] = (403, "Forbidden: bot was blocked by the user")
    c, d = cli("telegram", "test")
    _, st = cli("telegram", "status")
    le = st["telegram"]["last_error"]
    check("П4 ошибка при блокировке", c != 0 and le and "заблокировал бота" in le["text"], le and le["text"])
    del tg.fail["sendMessage"]
    c, d = cli("telegram", "test")
    _, st = cli("telegram", "status")
    check("П4 ошибка ушла", c == 0 and st["telegram"]["last_error"] is None)

    # П5: токен не виден
    secret_part = TOKEN.split(":")[1][:10]
    found = [str(p) for p in HOME.rglob("*") if p.is_file() and secret_part.encode() in p.read_bytes()]
    _, cfg = cli("config", "show")
    check("П5 токена нет в файлах и config show", not found and secret_part not in json.dumps(cfg), ", ".join(found))


# --- общие для запусков ---------------------------------------------------------


def schedule(preset: str) -> tuple[str, str]:
    """Ставит расписание и возвращает (id, команда из фейкового crontab без cron-выражения)."""
    c, d = cli("schedule", "add", "telegram-demo", "--preset", preset, "--cron", "*/5 * * * *", check=True)
    sid = d["schedule"]["id"]
    line = next(x for x in (WORK / "crontab").read_text(encoding="utf-8").splitlines() if sid in x)
    return sid, line.split(" ", 5)[5].split(" # ")[0]


def cron(cmd: str, background: bool = False):
    p = subprocess.Popen(["/bin/sh", "-c", cmd])
    return p if background else p.wait(timeout=600)


def newest_run() -> str:
    with app() as a:
        return a.runs.list("telegram-demo", 1)[0].id


def result_text(since: int, timeout: float = 20, head: tuple[str, ...] = ("Готово", "Упал")) -> str | None:
    return wait_for(lambda: next((t for t in reversed(texts(since)) if t.startswith(head)), None), timeout)


def has_all(text: str | None, *parts: str) -> bool:
    return bool(text) and all(p in text for p in parts)


def cb_answers(since: int) -> list[str]:
    return [p.get("text", "") for p in sent(since, "answerCallbackQuery")]


def edits(since: int) -> list[str]:
    return [p["text"] for p in sent(since, "editMessageText")]


def q_mid_for_new_run(since_runs: set[str], timeout: float = 60) -> tuple[str, int] | None:
    def find():
        for k, mid in link_mids().items():
            rid = k.split(":")[0]
            if rid not in since_runs and mid is not None:
                return rid, mid
        return None
    return wait_for(find, timeout)


def known_runs() -> set[str]:
    with app() as a:
        return {r.id for r in a.runs.list("telegram-demo", 500)}


def keyboard_of(mid_since: int) -> list[tuple[str, str]]:
    q = [p for p in sent(mid_since) if p.get("reply_markup")]
    return [(b["text"], b["callback_data"]) for row in q[-1]["reply_markup"]["inline_keyboard"] for b in row] if q else []


# --- Р --------------------------------------------------------------------------


def s_R() -> None:

    sid, cmd = schedule("ок")
    m0 = mark()
    cron(cmd)
    t = result_text(m0)
    check("Р1 итог регулярного", has_all(t, "Готово: Демо уведомлений в Telegram, пресет «ок»", "собрано 50", "Собрано записей: 50",
                                         "Как устроены уведомления: https://core.telegram.org/bots", "Шёл ", "В приложении: Запуски, "), t)
    check("Р1 без адреса приложения и токена", t and "127.0.0.1" not in t and "token" not in t)
    log_msgs("Р1", m0)
    cli("schedule", "remove", sid)

    sid, cmd = schedule("да-нет")
    before, m0 = known_runs(), mark()
    p = cron(cmd, background=True)
    got = q_mid_for_new_run(before)
    q = texts(m0)
    kb = keyboard_of(m0)
    check("Р2 вопрос", got and q and has_all(q[-1], "Демо уведомлений в Telegram · да-нет", "Вопрос: Демо: продолжить запуск?",
                                              "Нажмите кнопку с ответом.", "Ответить до ") and [k for k, _ in kb] == ["Да", "Нет"],
          q[-1] if q else "")
    yes = next(d for k, d in kb if k == "Да")
    push_cb(got[1], yes, "cb1")
    p.wait(timeout=120)
    t = result_text(m0)
    check("Р2 кнопка и итог", has_all(t, "Готово: Демо уведомлений в Telegram, пресет «да-нет»", "подтверждено")
          and any(e.endswith("Ответ принят: да") for e in edits(m0)) and "Ответ принят" in cb_answers(m0), f"{edits(m0)} {cb_answers(m0)}")
    no_kb = all("reply_markup" not in e for e in sent(m0, "editMessageText"))
    check("Р2 кнопки убраны после ответа", no_kb)
    log_msgs("Р2", m0)
    cli("schedule", "remove", sid)

    sid, cmd = schedule("код")
    before, m0 = known_runs(), mark()
    p = cron(cmd, background=True)
    got = q_mid_for_new_run(before)
    q = texts(m0)
    check("Р3 вопрос с кодом", got and has_all(q[-1], "Вопрос: Демо: введите любой код", "Чтобы ответить, ответьте на это сообщение (Ответить).",
                                               "Ответ секретный: бот сразу удалит ваше сообщение с ним."), q[-1] if q else "")
    code_mid = push_reply("48151623", got[1])
    p.wait(timeout=120)
    t = result_text(m0)
    deleted = [d["message_id"] for d in sent(m0, "deleteMessage")]
    found = [str(f) for f in HOME.rglob("*") if f.is_file() and b"48151623" in f.read_bytes()]
    check("Р3 код удалён, принят, нигде не лежит", code_mid in deleted and has_all(t, "код получен (8 симв.)")
          and any(e.endswith("Ответ принят") for e in edits(m0)) and not found, f"deleted={deleted} found={found} {edits(m0)}")
    log_msgs("Р3", m0)
    cli("schedule", "remove", sid)

    before, m0 = known_runs(), mark()
    rid = start("--set", "ask=confirm", "--set", "ask_minutes=1")
    got = q_mid_for_new_run(before)
    wait_run(rid, 150)
    t = result_text(m0)
    no_kb = all("reply_markup" not in e for e in sent(m0, "editMessageText"))
    check("Р4 вопрос без ответа", has_all(t, "Упал: Демо уведомлений в Telegram", "Не дождались ответа на вопрос «Демо: продолжить запуск?».")
          and any("Срок ответа вышел" in e for e in edits(m0)) and no_kb, f"{t!r} {edits(m0)}")
    log_msgs("Р4 (ручной в фоне, ask_minutes=1)", m0)


# --- В --------------------------------------------------------------------------


def s_V() -> None:
    before, m0 = known_runs(), mark()
    rid = start("--set", "ask=choice", "--notify")
    got = q_mid_for_new_run(before)
    kb = keyboard_of(m0)
    check("В1 кнопки выбора", [k for k, _ in kb] == ["1. Москва", "2. Казань", "3. Петербург"], kb)
    push_cb(got[1], kb[1][1], "c1")
    wait_run(rid)
    t = result_text(m0)
    check("В1 выбор кнопкой", has_all(t, "выбран город Казань"), t)
    log_msgs("В1 выбор", m0)

    before, m0 = known_runs(), mark()
    rid = start("--set", "ask=text", "--notify")
    got = q_mid_for_new_run(before)
    push_reply("привет", None)
    wait_run(rid)
    t = result_text(m0)
    check("В1 простое сообщение при одном вопросе", has_all(t, "ответ «привет»"), t)
    log_msgs("В1 текст", m0)

    before, m0 = known_runs(), mark()
    r1 = start("--set", "ask=text", "--notify")
    g1 = q_mid_for_new_run(before)
    r2 = start("--set", "ask=text", "--notify")
    g2 = q_mid_for_new_run(before | {r1})
    m1 = mark()
    push_reply("раз", None)
    sev = wait_for(lambda: [x for x in texts(m1) if x.startswith("Ответа ждут несколько")], 10)
    push_reply("раз", g1[1])
    push_reply("два", g2[1])
    wait_run(r1), wait_run(r2)
    with app() as a:
        s1, s2 = a.runs.get(r1).summary, a.runs.get(r2).summary
    check("В2 несколько вопросов", sev and "ответ «раз»" in s1 and "ответ «два»" in s2, f"{sev} | {s1} | {s2}")
    log_msgs("В2", m0)

    before, m0 = known_runs(), mark()
    rid = start("--set", "ask=confirm", "--notify")
    got = q_mid_for_new_run(before)
    cli("answer", rid, "yes")
    kb = keyboard_of(m0)
    m1 = mark()
    push_cb(got[1], kb[1][1], "c9")
    wait_run(rid)
    ans = wait_for(lambda: cb_answers(m1), 10)
    t = result_text(m0)
    check("В3 первый ответ засчитан", has_all(t, "подтверждено") and ans and ans[0] in ("Уже отвечено", "Вопрос закрыт"), f"{ans} {t!r}")
    log_msgs("В3", m0)


# --- Ч --------------------------------------------------------------------------


def run_and_texts(*args: str, wait: float = 6) -> tuple[str, list[str]]:
    m0 = mark()
    rid = start(*args)
    wait_run(rid, 200)
    time.sleep(wait)
    return rid, texts(m0)


def s_C() -> None:
    cli("telegram", "set", "long_minutes", "1", check=True)
    m0 = mark()
    short = start("--set", "minutes=0.2")
    long = start("--set", "minutes=1.1")
    wait_run(short, 200), wait_run(long, 200)
    time.sleep(6)
    ts = texts(m0)
    check("Ч1 короткий молчит, долгий пишет", len([t for t in ts if t.startswith("Готово")]) == 1 and any("Шёл 1 мин" in t for t in ts), ts)
    log_msgs("Ч1", m0)

    cli("telegram", "set", "done_manual", "always", check=True)
    _, t1 = run_and_texts("--no-notify")
    cli("telegram", "set", "done_manual", "long", check=True)
    _, t2 = run_and_texts("--notify")
    _, t3 = run_and_texts("--set", "outcome=failed", "--no-notify")
    check("Ч2 --no-notify молчит", not t1, t1)
    check("Ч2 --notify пишет", any(t.startswith("Готово") for t in t2), t2)
    check("Ч2 падение и при --no-notify", any(t.startswith("Упал") for t in t3), t3)

    _, t = run_and_texts("--set", "outcome=failed")
    check("Ч3 падение с объяснением", t and has_all(t[-1], "Упал: Демо уведомлений в Telegram", "сайт демо не ответил"), t)
    m0 = mark()
    _, t = run_and_texts("--set", "outcome=crash")
    check("Ч3 падение без итога", t and t[-1].startswith("Упал:"), t)
    log_msgs("Ч3 failed/crash", m0 - 0)

    cli("config", "set", "max_parallel_runs", "1", check=True)
    cli("config", "set", "wait_slot_minutes", "1", check=True)
    sid, cmd = schedule("ок")
    m0 = mark()
    busy = start("--set", "minutes=1.6")
    cron(cmd)
    t = result_text(m0, 30, ("Упал",))
    check("Ч3 пропущенный плановый", has_all(t, "Упал: Демо уведомлений в Telegram, пресет «ок»", "Не начался: все места для одновременных запусков были заняты, а время ожидания вышло."), t)
    log_msgs("Ч3 skipped", m0)
    cli("schedule", "remove", sid)
    wait_run(busy, 200)
    cli("config", "set", "max_parallel_runs", "3", check=True)
    cli("config", "set", "wait_slot_minutes", "30", check=True)

    m0 = mark()
    _, t = run_and_texts("--set", "outcome=partial", "--notify")
    check("Ч4 частичный", t and has_all(t[-1], "Готово частично:", "3 из 5 строк, 2 упали", "Собрано записей: 3"), t)
    log_msgs("Ч4", m0)

    m0 = mark()
    _, t = run_and_texts("--set", "count=0", "--notify")
    check("Ч5 тихая поломка", t and "Похоже, сайт поменялся. Запуск завершился без ошибки, но скрипт сообщил, что собрал 0 записей." in t[-1], t)
    log_msgs("Ч5", m0)
    cli("telegram", "set", "suspicious", "off", check=True)
    _, t = run_and_texts("--set", "count=0", "--notify")
    check("Ч5 галочка убирает строку", t and "Похоже" not in t[-1], t)
    cli("telegram", "set", "suspicious", "on", check=True)

    cli("telegram", "set", "attach_main", "on", check=True)
    m0 = mark()
    run_and_texts("--set", "count=200", "--notify")
    docs = tg.documents[-1:] if sent(m0, "sendDocument") else []
    check("Ч6 файл документом", docs and docs[0]["document"]["filename"] == "result.csv"
          and docs[0]["document"]["content"].count(b"\n") == 201, docs and docs[0].get("caption"))
    cli("telegram", "set", "attach_main", "off", check=True)
    log_msgs("Ч6", m0)

    m0 = mark()
    rid = start("--set", "minutes=3", "--notify")
    time.sleep(4)
    cli("cancel", rid)
    wait_run(rid)
    p = bg("run", "telegram-demo", "--set", "minutes=3", "--notify", "--follow")
    time.sleep(5)
    p.send_signal(signal.SIGINT)
    p.wait(timeout=60)
    time.sleep(5)
    with app() as a:
        reasons = [r.reason for r in a.runs.list("telegram-demo", 2)]
    check("Ч7 отмена молчит (cancel и Ctrl+C)", not texts(m0) and reasons == ["cancelled", "cancelled"], f"{texts(m0)} {reasons}")


# --- О --------------------------------------------------------------------------


def s_O() -> None:
    other = WORK / "other.py"
    other.write_text((ROOT / "examples/telegram-demo/script.py").read_text(encoding="utf-8")
                     .replace('name = "telegram-demo"', 'name = "telegram-other"')
                     .replace('title = "Демо уведомлений в Telegram"', 'title = "Другой скрипт"'), encoding="utf-8")
    cli("add", str(other), check=True)
    cli("telegram", "set", "done_manual", "always", check=True)
    c, d = cli("telegram", "set", "done_manual", "never", "--script", "telegram-demo")
    check("О1 своё у скрипта", c == 0 and d["own"] == {"done_manual": "never"}, d)
    _, t1 = run_and_texts()
    _, t2 = run_and_texts("--notify")
    m0 = mark()
    c, d = cli("run", "telegram-other", "--follow")
    time.sleep(5)
    t3 = texts(m0)
    check("О1 скрипт: молчит, --notify главнее, другой скрипт пишет",
          not t1 and any(t.startswith("Готово") for t in t2) and any(t.startswith("Готово: Другой скрипт") for t in t3), f"{t1} {t2} {t3}")
    p = subprocess.run([DR, "telegram", "set", "--script", "telegram-demo"], capture_output=True, text=True)
    check("О1 вывод telegram set --script", "(своё)" in p.stdout and "(наследуется)" in p.stdout, p.stdout)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("\n===== О1 telegram set --script\n" + p.stdout)
    c, d = cli("telegram", "set", "done_manual", "inherit", "--script", "telegram-demo")
    check("О1 inherit", c == 0 and d["own"] == {}, d)
    cli("telegram", "set", "done_manual", "long", check=True)

    sid, cmd = schedule("ок")
    sid2, cmd2 = schedule("плохо")
    cli("telegram", "set", "done_scheduled", "off", "--schedule", sid, check=True)
    cli("telegram", "set", "done_scheduled", "off", "--schedule", sid2, check=True)
    cli("telegram", "set", "done_scheduled", "on", "--script", "telegram-demo", check=True)
    m0 = mark()
    cron(cmd)
    time.sleep(5)
    t1 = texts(m0)
    m0 = mark()
    cron(cmd2)
    t2 = result_text(m0)
    c, d = cli("telegram", "set", "done_manual", "never", "--schedule", sid)
    check("О2 расписание главнее скрипта, падение приходит", not t1 and has_all(t2, "Упал:", "пресет «плохо»"), f"{t1} {t2!r}")
    check("О2 у расписания нет done_manual", c != 0, d.get("error", {}).get("message"))
    for s in (sid, sid2):
        cli("telegram", "set", "done_scheduled", "inherit", "--schedule", s)
        cli("schedule", "remove", s)
    cli("telegram", "set", "done_scheduled", "inherit", "--script", "telegram-demo")


# --- Н --------------------------------------------------------------------------


def s_N() -> None:
    with app() as a:
        a.set_config("telegram_api_url", "https://127.0.0.1:9")
    t0 = time.monotonic()
    c, d = cli("run", "telegram-demo", "--notify", "--follow")
    took = time.monotonic() - t0
    rid = d["run"]["id"]
    _, logs = cli("logs", rid, "--events")
    warn = [e.get("msg", "") for e in logs.get("events", []) if e.get("t") == "log" and "Telegram" in str(e.get("msg", ""))]
    _, st = cli("telegram", "status")
    check("Н1 запуск не сломан и не ждал", c == 0 and d["run"]["label"] == "ok" and took < 30, f"{took:.1f} с")
    check("Н1 ошибка в журнале и в статусе", warn and st["telegram"]["last_error"], f"{warn} | {st['telegram']['last_error']}")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== Н1 журнал\n{warn}\n{st['telegram']['last_error']}\n")
    rid = start("--set", "ask=confirm")
    from dawg_replay import asks
    wait_for(lambda: asks.waiting(HOME / "runs" / rid), 30)
    c2, d2 = cli("answer", rid, "yes")
    rec = wait_run(rid, 60)
    check("Н1 вопрос отвечается из приложения при недоступном Telegram", c2 == 0 and rec.status == "ok", f"{d2} {rec.status}")
    with app() as a:
        a.set_config("telegram_api_url", tg.url)
    m0 = mark()
    cli("runs")
    time.sleep(3)
    cli("runs")
    time.sleep(3)
    t = [x for x in texts(m0) if x.startswith("Готово")]
    check("Н1 досылка после восстановления без дублей", len(t) == 1, t)


# --- Д --------------------------------------------------------------------------


def s_D() -> None:
    c, d = cli("telegram", "disconnect")
    _, st = cli("telegram", "status")
    m0 = mark()
    c2, d2 = cli("run", "telegram-demo", "--set", "outcome=failed", "--follow")
    rid = d2["run"]["id"]
    _, logs = cli("logs", rid, "--events")
    warn = [e for e in logs.get("events", []) if e.get("t") == "log" and "Telegram" in str(e.get("msg", ""))]
    check("Д1 отключение", c == 0 and not st["telegram"]["connected"] and KR.get_password("dawg-replay", "telegram-bot-token") is None)
    check("Д1 запуски без сообщений и ошибок", not tg.calls[m0:] and not warn, f"{tg.calls[m0:]} {warn}")


# --- UI (TestClient, тот же home) ----------------------------------------------


def s_UI() -> None:
    import html as _html

    from dawg_replay.web.security import Secrets
    from dawg_replay.web.server import create_app
    from starlette.testclient import TestClient

    port = 54350
    base = f"http://127.0.0.1:{port}"
    sec = Secrets(port=port)
    cli("telegram", "set", "long_minutes", "10", check=True)
    sid, _ = schedule("ок")
    m0 = mark()
    with TestClient(create_app(HOME, sec, keyring_backend=KR), base_url=base, follow_redirects=False) as c:
        c.get(f"/?token={sec.token}")
        h = {"Origin": base, "X-CSRF-Token": sec.csrf}
        st = _html.unescape(c.get("/settings").text)
        check("UI Настройки: подключено и правила", "Подключено: бот @replay_test_bot" in st and "О чём писать" in st
              and "Ручной запуск закончился" in st and "если дольше порога" in st, "")
        sp = _html.unescape(c.get("/scripts/telegram-demo").text)
        check("UI форма: выбор у запуска", "Сообщить в Telegram, когда закончится" in sp and "по настройкам: " in sp,
              sp[sp.find("по настройкам: "):sp.find("по настройкам: ") + 60])
        check("UI страница скрипта: блок", "Уведомления в Telegram" in sp and "как в общих настройках" in sp)
        sch = _html.unescape(c.get("/scripts/telegram-demo/_schedules").text)
        check("UI расписание: блок", "Уведомления в Telegram" in sch and ", как у скрипта" in sch and "Ручной запуск закончился" not in sch)
        with open(LOG, "a", encoding="utf-8") as f:
            i = sp.find("по настройкам: ")
            f.write("\n===== UI форма\n" + sp[i:i + 80] + "\n")
        r = c.post("/scripts/telegram-demo/run", data={"_notify": "always", "outcome": "ok", "minutes": "0", "ask": "none",
                                                       "ask_minutes": "10", "count": "5"}, headers=h)
        rid = r.headers.get("hx-redirect", "").rsplit("/", 1)[-1]
        wait_run(rid)
        t = result_text(m0)
        check("UI запуск «всегда» пишет", has_all(t, "Готово:", "собрано 5"), f"{r.status_code} {t!r}")
        m1 = mark()
        r = c.post("/scripts/telegram-demo/run", data={"_notify": "always", "outcome": "ok", "minutes": "3", "ask": "none",
                                                       "ask_minutes": "10", "count": "5"}, headers=h)
        rid2 = r.headers.get("hx-redirect", "").rsplit("/", 1)[-1]
        time.sleep(3)
    wait_run(rid2, 60)
    time.sleep(4)
    rec = run_rec(rid2)
    check("UI остановка сервера молчит", not texts(m1) and rec.reason == "cancelled", f"{texts(m1)} {rec.status}/{rec.reason}")
    cli("schedule", "remove", sid)


SCENARIOS = {"P": s_P, "R": s_R, "V": s_V, "C": s_C, "O": s_O, "N": s_N, "UI": s_UI, "D": s_D}


def main() -> None:
    names = sys.argv[1].split(",") if len(sys.argv) > 1 and sys.argv[1] != "all" else list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        sys.exit(f"нет сценариев {', '.join(unknown)}; есть: {', '.join(SCENARIOS)}")
    tg.start()
    cli("add", str(ROOT / "examples/telegram-demo/script.py"), check=True)
    cli("preset", "save", "telegram-demo", "ок", "--set", "outcome=ok", "--set", "minutes=0.02", check=True)
    cli("preset", "save", "telegram-demo", "да-нет", "--set", "ask=confirm", "--set", "ask_minutes=10", check=True)
    cli("preset", "save", "telegram-demo", "код", "--set", "ask=code", "--set", "ask_minutes=10", check=True)
    cli("preset", "save", "telegram-demo", "плохо", "--set", "outcome=failed", check=True)
    try:
        for n in names:
            SCENARIOS[n]()
    finally:
        tg.stop()
        bad = [r for r in RESULTS if not r[1]]
        print(f"\nитог: {len(RESULTS) - len(bad)} ок, {len(bad)} нет; сообщения: {LOG}")
        for r in bad:
            print("  НЕТ", r[0], r[2])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
