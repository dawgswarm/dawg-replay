"""Вопросы скрипта в Telegram (dr-99): сообщение о вопросе, ответ оттуда, один опрашивающий.

Сети нет: фейковый Bot API (`FakeServer`/`FakeBot`) с семантикой getUpdates (offset
подтверждает прочитанное, параллельный вызов даёт конфликт 409, как у Telegram).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dawg_replay import asks, launch, telegram, tg_asks, tg_updates
from dawg_replay.errors import ReplayError

CHAT = 4242
OTHER = 777


class TelegramError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code, self.description = code, description


class FakeServer:
    """Один бот на всех: обновления, учёт сообщений, ловушка параллельного getUpdates."""

    def __init__(self):
        self.lock = threading.Lock()
        self.updates: list[dict] = []
        self.confirmed = 0
        self.next_update = 1
        self.next_message = 100
        self.calls: list[tuple] = []
        self.active_polls = 0
        self.conflicts = 0
        self.polls = 0
        self.fail_delete = False
        self.fail_send = False

    def record(self, *call):
        with self.lock:
            self.calls.append(call)

    def push(self, update: dict) -> None:
        with self.lock:
            update = {"update_id": self.next_update, **update}
            self.next_update += 1
            self.updates.append(update)

    def sent(self, kind="send"):
        with self.lock:
            return [c for c in self.calls if c[0] == kind]

    def all_text(self) -> str:
        with self.lock:
            return json.dumps(self.calls, ensure_ascii=False, default=str)


class FakeBot:
    def __init__(self, server: FakeServer):
        self.s = server

    def send_message(self, chat_id, text, reply_markup=None):
        if self.s.fail_send:
            raise TelegramError(403, "Forbidden: bot was blocked by the user")
        with self.s.lock:
            mid = self.s.next_message
            self.s.next_message += 1
        self.s.record("send", chat_id, text, reply_markup, mid)
        return mid

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.s.record("edit", chat_id, message_id, text, reply_markup)

    def delete_message(self, chat_id, message_id):
        self.s.record("delete", chat_id, message_id)
        if self.s.fail_delete:
            raise TelegramError(400, "Bad Request: message can't be deleted")
        return True

    def answer_callback_query(self, cq_id, text=None):
        self.s.record("cb", cq_id, text)

    def get_updates(self, offset, timeout):
        with self.s.lock:
            if self.s.active_polls:
                self.s.conflicts += 1
                raise TelegramError(409, "Conflict: terminated by other getUpdates request")
            self.s.active_polls += 1
            self.s.polls += 1
        try:
            end = time.monotonic() + min(timeout, 0.3)
            while True:
                with self.s.lock:
                    if offset is not None:
                        self.s.confirmed = max(self.s.confirmed, offset - 1)
                    out = [u for u in self.s.updates if u["update_id"] > self.s.confirmed]
                if out or time.monotonic() >= end:
                    return out
                time.sleep(0.02)
        finally:
            with self.s.lock:
                self.s.active_polls -= 1


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def ask_event(ask_id="1-abc", question="Код из SMS", kind="text", secret=False, choices=None, minutes=10):
    e = {"t": "ask", "ts": iso(datetime.now(timezone.utc)), "id": ask_id, "question": question, "kind": kind,
         "secret": secret, "deadline": iso(datetime.now(timezone.utc) + timedelta(minutes=minutes))}
    if choices:
        e["choices"] = choices
    return e


def write_events(run_dir: Path, *events):
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "events.jsonl", "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("не дождались")


def callback(mid, data, chat=CHAT, cq_id="cq1"):
    return {"callback_query": {"id": cq_id, "data": data, "from": {"id": chat},
                               "message": {"message_id": mid, "chat": {"id": chat}}}}


def message(text, chat=CHAT, mid=900, reply_to=None):
    m = {"message_id": mid, "chat": {"id": chat}, "text": text}
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"message": m}


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(tg_asks, "IDLE_SECONDS", 0.05)
    monkeypatch.setattr(tg_asks, "ERROR_PAUSE", 0.05)
    monkeypatch.setattr(tg_updates, "_RETRY", 0.02)


def run_asks(home, server, run_id="r1", **kw):
    return tg_asks.RunAsks(home, run_id, FakeBot(server), CHAT, title=kw.pop("title", "Ozon: карточки"), **kw)


def ask_and_send(home, server, ra, *events):
    write_events(Path(home) / "runs" / ra.run_id, *events)
    before = len(server.sent())
    assert ra.tick() == []
    wait_for(lambda: len(server.sent()) >= before + len(events))
    return server.sent()[before:]


def key_of(send_call):
    markup = send_call[3]
    return markup["inline_keyboard"][0][0]["callback_data"].split(":")[1]


# --- атомарный первый ответ ----------------------------------------------------------------


def _race(run_dir, q, n=16):
    barrier = threading.Barrier(n)
    won, lost = [], []

    def go(i):
        barrier.wait()
        try:
            asks.write_answer(run_dir, q, f"ответ {i}")
            won.append(i)
        except ReplayError as e:
            assert e.code == "already_answered"
            lost.append(i)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return won, lost


def test_write_answer_first_wins_race(tmp_path):
    q = asks.parse_ask(ask_event())
    for attempt in range(5):
        run_dir = tmp_path / f"run{attempt}"
        won, lost = _race(run_dir, q)
        assert len(won) == 1 and len(lost) == 15
        data = json.loads(asks.answer_path(run_dir, q.id).read_text(encoding="utf-8"))
        assert data["answer"] == f"ответ {won[0]}"  # в файле ответ победителя, не перезаписан
        assert [p.name for p in (run_dir / "answers").iterdir()] == [f"{q.id}.json"]  # без временных


def test_write_answer_first_wins_across_processes(tmp_path):
    run_dir = tmp_path / "run"
    q = asks.parse_ask(ask_event())
    code = textwrap.dedent('''
        import sys, time
        from dawg_replay import asks
        from dawg_replay.errors import ReplayError
        q = asks.Question("1-abc", "Код", "text", None, False, "2099-01-01T00:00:00Z")
        start = float(sys.argv[2])
        while time.time() < start:
            pass
        try:
            asks.write_answer(sys.argv[1], q, sys.argv[3])
            print("won")
        except ReplayError as e:
            print(e.code)
    ''')
    start = time.time() + 2.0
    procs = [subprocess.Popen([sys.executable, "-c", code, str(run_dir), str(start), f"p{i}"],
                              stdout=subprocess.PIPE, text=True) for i in range(6)]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert sorted(outs) == ["already_answered"] * 5 + ["won"]
    assert asks.has_answer(run_dir, q.id)


def test_write_answer_without_hard_links(tmp_path, monkeypatch):
    def no_link(*a, **k):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    q = asks.parse_ask(ask_event())
    won, lost = _race(tmp_path / "run", q)
    assert len(won) == 1 and len(lost) == 15
    asks.drop_secret_answers(tmp_path / "run")  # служебная метка убирается вместе с временными
    assert [p.name for p in (tmp_path / "run" / "answers").iterdir()] == [f"{q.id}.json"]


# --- блокировка опрашивающего ----------------------------------------------------------------


def test_updates_lock_exclusive_and_offset(tmp_path):
    a, b = tg_updates.UpdatesLock(tmp_path), tg_updates.UpdatesLock(tmp_path)
    assert a.acquire(0) and not b.acquire(0) and not b.acquire(0.1)
    a.release()
    assert b.acquire(0)
    b.release()
    with tg_updates.updates_lock(tmp_path, wait=0) as got:
        assert got
        with tg_updates.updates_lock(tmp_path, wait=0) as again:
            assert not again
    assert tg_updates.load_offset(tmp_path) is None
    tg_updates.save_offset(tmp_path, 15)
    assert tg_updates.load_offset(tmp_path) == 15
    assert (tmp_path / "telegram" / "offset").read_text() == "15"


@pytest.mark.skipif(sys.platform == "win32", reason="kill -9 держателя проверяется на POSIX")
def test_updates_lock_taken_over_when_holder_dies(tmp_path):
    code = textwrap.dedent('''
        import sys, time
        from dawg_replay import tg_updates
        lock = tg_updates.UpdatesLock(sys.argv[1])
        assert lock.acquire(0)
        print("held", flush=True)
        time.sleep(60)
    ''')
    p = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        lock = tg_updates.UpdatesLock(tmp_path)
        assert not lock.acquire(0)
        p.kill()
        p.wait(10)
        assert lock.acquire(5)
        lock.release()
    finally:
        p.kill()


# --- тексты -----------------------------------------------------------------------------


def test_question_text_and_keyboards():
    q = asks.parse_ask(ask_event(question="Отправить заказ? token=abc123secret", kind="confirm"))
    text = tg_asks.question_text("Ozon: карточки", "склад", q, ["abc123secret"])
    assert text.startswith("Ozon: карточки · склад\n") and "abc123secret" not in text
    assert "Нажмите кнопку" in text and "Ответить до" in text
    kb = tg_asks.keyboard("k1", q)
    assert [b["callback_data"] for b in kb["inline_keyboard"][0]] == ["a:k1:y", "a:k1:n"]
    q = asks.parse_ask(ask_event(kind="choice", choices=["Москва", "Тула " * 30]))
    kb = tg_asks.keyboard("k1", q)
    assert [r[0]["callback_data"] for r in kb["inline_keyboard"]] == ["a:k1:1", "a:k1:2"]
    assert all(len(r[0]["text"]) <= tg_asks.BUTTON_TEXT + 3 for r in kb["inline_keyboard"])
    assert all(len(r[0]["callback_data"].encode()) <= 64 for r in kb["inline_keyboard"])
    q = asks.parse_ask(ask_event(secret=True))
    text = tg_asks.question_text("T", None, q)
    assert "ответьте на это сообщение" in text and "сразу удалит" in text
    assert tg_asks.keyboard("k1", q) is None


# --- ответы ----------------------------------------------------------------------------------


def test_confirm_button_answer_then_already_and_edit(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    try:
        send = ask_and_send(tmp_path, server, ra, ask_event(kind="confirm", question="Отправить?"))[0]
        assert send[1] == CHAT and "Отправить?" in send[2]
        key = key_of(send)
        server.push(callback(send[4], f"a:{key}:y"))
        q = asks.read(tmp_path / "runs" / "r1" / "events.jsonl")[0]
        wait_for(lambda: asks.has_answer(tmp_path / "runs" / "r1", q.id))
        data = json.loads(asks.answer_path(tmp_path / "runs" / "r1", q.id).read_text(encoding="utf-8"))
        assert data["answer"] is True
        wait_for(lambda: server.sent("cb"))
        assert server.sent("cb")[0][2] == tg_asks.T_ACCEPTED
        edits = wait_for(lambda: server.sent("edit"))
        assert edits[0][2] == send[4] and edits[0][3].endswith("Ответ принят: да") and edits[0][4] is None
        # Вторая кнопка (повторное нажатие или ответ из UI раньше): уже отвечено. Вопросов
        # больше нет, опрос стих, так что такое нажатие придёт со следующим опросом.
        ra.dispatcher.handle(callback(send[4], f"a:{key}:n", cq_id="cq2"))
        assert server.sent("cb")[1] == ("cb", "cq2", tg_asks.T_ALREADY)
        # Скрипт закрыл вопрос: сообщение уже поправлено, повторной правки нет.
        write_events(tmp_path / "runs" / "r1", {"t": "ask_closed", "id": q.id, "outcome": "answered", "answer": True})
        ra.tick()
    finally:
        ra.close()
    assert len(server.sent("edit")) == 1
    assert tg_asks.load_links(tmp_path) == []  # связи запуска убраны


def test_choice_button_and_closed_question(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    try:
        send = ask_and_send(tmp_path, server, ra, ask_event(kind="choice", choices=["Москва", "Тула"]))[0]
        key = key_of(send)
        server.push(callback(send[4], f"a:{key}:2"))
        run_dir = tmp_path / "runs" / "r1"
        wait_for(lambda: asks.has_answer(run_dir, "1-abc"))
        assert json.loads(asks.answer_path(run_dir, "1-abc").read_text(encoding="utf-8"))["answer"] == "Тула"
        # Нажатие на неизвестный ключ (запуск давно кончился): вопрос закрыт.
        ra.dispatcher.handle(callback(send[4], "a:deadbeef00:1", cq_id="cq-old"))
        assert ("cb", "cq-old", tg_asks.T_CLOSED) in server.sent("cb")
    finally:
        ra.close()


def test_timeout_and_run_end_edit_message(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    run_dir = tmp_path / "runs" / "r1"
    try:
        s1, s2 = ask_and_send(tmp_path, server, ra, ask_event("a1", kind="confirm"), ask_event("a2", kind="text"))
        write_events(run_dir, {"t": "ask_closed", "id": "a1", "outcome": "timeout"})
        ra.tick()
        edits = wait_for(lambda: server.sent("edit"))
        assert edits[0][2] == s1[4] and edits[0][3].endswith(tg_asks.T_TIMEOUT)
    finally:
        ra.close()
    edits = server.sent("edit")
    assert edits[-1][2] == s2[4] and edits[-1][3].endswith(tg_asks.T_RUN_ENDED)


def test_text_reply_and_single_waiting_plain_message(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    run_dir = tmp_path / "runs" / "r1"
    try:
        s1, s2 = ask_and_send(tmp_path, server, ra, ask_event("a1", question="Город?"),
                              ask_event("a2", question="Индекс?"))
        # Два ждущих: простое сообщение не к чему отнести.
        server.push(message("Тула", mid=901))
        wait_for(lambda: any(tg_asks.T_SEVERAL in c[2] for c in server.sent()))
        assert not asks.has_answer(run_dir, "a1") and not asks.has_answer(run_dir, "a2")
        # Ответ на сообщение (reply) находит свой вопрос.
        server.push(message("  Москва ", mid=902, reply_to=s2[4]))
        wait_for(lambda: asks.has_answer(run_dir, "a2"))
        assert json.loads(asks.answer_path(run_dir, "a2").read_text(encoding="utf-8"))["answer"] == "Москва"
        # Остался один: простое сообщение идёт ему.
        server.push(message("Тула", mid=903))
        wait_for(lambda: asks.has_answer(run_dir, "a1"))
        assert json.loads(asks.answer_path(run_dir, "a1").read_text(encoding="utf-8"))["answer"] == "Тула"
        # Команды и пустые сообщения не ответы.
        server.push(message("/start abc", mid=904))
    finally:
        ra.close()
    assert not server.sent("delete")  # несекретные ответы не удаляются


def test_secret_answer_deleted_and_never_stored(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    run_dir = tmp_path / "runs" / "r1"
    try:
        (s1,) = ask_and_send(tmp_path, server, ra, ask_event("a1", secret=True))
        server.push(message("482913", mid=950, reply_to=s1[4]))
        wait_for(lambda: asks.has_answer(run_dir, "a1"))
        wait_for(lambda: server.sent("edit"))
        assert ("delete", CHAT, 950) in server.sent("delete")
        assert server.sent("edit")[0][3].endswith("\n\nОтвет принят")  # без значения
        links_text = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "telegram" / "asks").glob("*.json"))
        assert "482913" not in links_text
        # Ответ через reply ещё раз: уже отвечено, сообщение с кодом тоже удаляется.
        ra.dispatcher.handle(message("111111", mid=951, reply_to=s1[4]))
        assert ("delete", CHAT, 951) in server.sent("delete")
        assert any(c[2] == tg_asks.T_ALREADY + "." for c in server.sent())
    finally:
        ra.close()
    sent_out = [c for c in server.calls if c[0] in ("send", "edit", "cb")]
    assert "482913" not in json.dumps(sent_out, ensure_ascii=False)
    assert "482913" not in (run_dir / "events.jsonl").read_text(encoding="utf-8")


def test_secret_delete_failure_asks_to_delete(tmp_path, server, fast):
    server.fail_delete = True
    ra = run_asks(tmp_path, server)
    run_dir = tmp_path / "runs" / "r1"
    try:
        ask_and_send(tmp_path, server, ra, ask_event("a1", secret=True))
        server.push(message("482913", mid=960))  # единственный ждущий, без reply
        wait_for(lambda: asks.has_answer(run_dir, "a1"))
        msg = wait_for(lambda: [c for c in server.sent() if tg_asks.T_DELETE_YOURSELF in c[2]])
        assert msg[0][2] == f"{tg_asks.T_ACCEPTED}. {tg_asks.T_DELETE_YOURSELF}"
        errors = wait_for(lambda: ra.take_errors())
        assert any("удалить сообщение с кодом" in e for e in errors) and not any("482913" in e for e in errors)
    finally:
        ra.close()


def test_other_chat_ignored(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    run_dir = tmp_path / "runs" / "r1"
    try:
        (s1,) = ask_and_send(tmp_path, server, ra, ask_event("a1", kind="confirm"))
        server.push(callback(s1[4], f"a:{key_of(s1)}:y", chat=OTHER))
        server.push(message("да", chat=OTHER, reply_to=s1[4]))
        wait_for(lambda: server.confirmed >= 2)
        time.sleep(0.2)
        assert not asks.has_answer(run_dir, "a1")
        assert [c for c in server.calls if c[0] != "send"] == []  # ни ответа, ни подсказки чужому чату
        assert len(server.sent()) == 1
    finally:
        ra.close()


def test_bad_answer_gets_hint(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    try:
        (s1,) = ask_and_send(tmp_path, server, ra, ask_event("a1", kind="confirm"))
        server.push(message("может быть", reply_to=s1[4]))
        wait_for(lambda: any("yes или no" in c[2] for c in server.sent()))
        server.push(message("да", mid=902, reply_to=s1[4]))
        wait_for(lambda: asks.has_answer(tmp_path / "runs" / "r1", "a1"))
    finally:
        ra.close()


def test_answer_after_owner_died_is_closed(tmp_path, server):
    run_dir = tmp_path / "runs" / "r9"
    write_events(run_dir, ask_event("a1", kind="confirm"))
    link = tg_asks.Link(key="abcdef0123", run_id="r9", ask_id="a1", chat_id=CHAT, kind="confirm", secret=False,
                        text="Вопрос", message_id=5, pid=2 ** 22 + 12345, pid_started=1.0)
    tg_asks.save_link(tmp_path, link)
    d = tg_asks.Dispatcher(tmp_path, FakeBot(server), CHAT, lambda t: None)
    d.handle(callback(5, "a:abcdef0123:y"))
    assert server.sent("cb") == [("cb", "cq1", tg_asks.T_CLOSED)]
    assert not asks.has_answer(run_dir, "a1")


# --- один опрашивающий на установку --------------------------------------------------------------


def test_one_poller_serves_all_runs_and_hands_over(tmp_path, server, fast):
    r1, r2 = run_asks(tmp_path, server, "r1"), run_asks(tmp_path, server, "r2")
    try:
        (s1,) = ask_and_send(tmp_path, server, r1, ask_event("a1", kind="confirm"))
        (s2,) = ask_and_send(tmp_path, server, r2, ask_event("b1", kind="confirm"))
        wait_for(lambda: server.polls >= 3)
        # Ответ второму запуску, кто бы ни держал опрос.
        server.push(callback(s2[4], f"a:{key_of(s2)}:y", cq_id="x2"))
        wait_for(lambda: asks.has_answer(tmp_path / "runs" / "r2", "b1"))
        # Вопрос первого закрылся, запуск кончился: роль переходит ко второму без потерь.
        r1.close()
        write_events(tmp_path / "runs" / "r2", ask_event("b2", kind="confirm"))
        r2.tick()
        s3 = wait_for(lambda: len(server.sent()) >= 3 and server.sent()[2])
        server.push(callback(s3[4], f"a:{key_of(s3)}:n", cq_id="x3"))
        wait_for(lambda: asks.has_answer(tmp_path / "runs" / "r2", "b2"))
    finally:
        r1.close()
        r2.close()
    assert server.conflicts == 0  # getUpdates ни разу не звали параллельно
    assert tg_updates.load_offset(tmp_path) == server.next_update  # всё обработанное подтверждено


def test_polling_only_while_questions_wait(tmp_path, server, fast):
    ra = run_asks(tmp_path, server)
    try:
        ra.tick()
        time.sleep(0.3)
        assert server.polls == 0  # вопросов нет: не опрашиваем
        (s1,) = ask_and_send(tmp_path, server, ra, ask_event("a1", kind="confirm"))
        wait_for(lambda: server.polls >= 1)
        server.push(callback(s1[4], f"a:{key_of(s1)}:y"))
        wait_for(lambda: asks.has_answer(tmp_path / "runs" / "r1", "a1"))
        # Ответили: блокировка свободна для привязки чата, опрос стих.
        with tg_updates.updates_lock(tmp_path, wait=3) as got:
            assert got
            polls = server.polls
            time.sleep(0.3)
            assert server.polls == polls
    finally:
        ra.close()


def test_send_failure_reported_and_run_unaffected(tmp_path, server, fast):
    server.fail_send = True
    ra = run_asks(tmp_path, server)
    try:
        write_events(tmp_path / "runs" / "r1", ask_event("a1"))
        ra.tick()
        errors = wait_for(lambda: ra.take_errors())
        assert errors == ["не удалось отправить вопрос в Telegram: Forbidden: bot was blocked by the user"]
        assert tg_asks.load_links(tmp_path) == []
    finally:
        ra.close()
    tg_asks.journal(tmp_path / "runs" / "r1", errors)
    last = json.loads((tmp_path / "runs" / "r1" / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert last["t"] == "log" and last["level"] == "warn" and last["msg"].startswith("Telegram: не удалось")


# --- целиком через supervise ----------------------------------------------------------------------

SCRIPT = '''\
# /// script
# dependencies = ["dawg-replay-kit"]
# ///

# /// dawg-script
# contract = 1
# name = "tg-ask"
# title = "Вопрос в Telegram"
# version = 1
# ///

import dawg_replay_kit as kit

code = kit.ask("Код из SMS", secret=True, timeout_minutes=1)
ok = kit.ask("Отправить?", kind="confirm", timeout_minutes=1)
kit.log.info(f"код {len(code)} знаков, подтверждение {ok}")
kit.result("ok", "готово")
'''


@pytest.fixture
def real_tg(app, kr):
    """Настоящий telegram.py (connection, Bot по HTTP) против фейкового сервера Bot API."""
    from fake_telegram import TOKEN, FakeTelegram

    with FakeTelegram() as fake:
        app.set_config("telegram_api_url", fake.url)
        app.secrets.set_telegram_token(TOKEN)
        telegram._save(app, telegram_chat_id=str(CHAT), telegram_chat_name="Иван", telegram_bot="replay_test_bot")
        yield fake


def test_supervise_answers_from_telegram(cli, home, kr, app, tmp_path, real_tg, fast, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    f = tmp_path / "tg-ask.py"
    f.write_text(SCRIPT, encoding="utf-8")
    code, _ = cli("add", str(f))
    assert code == 0
    assert tg_asks.open_bot(app)[1] == CHAT

    def answer():
        s1 = wait_for(lambda: real_tg.sent() and real_tg.sent()[0], 30)
        assert s1["text"].startswith("Вопрос в Telegram\n") and "reply_markup" not in s1
        real_tg.push_message("482913", chat_id=CHAT)  # единственный ждущий: простым сообщением
        s2 = wait_for(lambda: len(real_tg.sent()) >= 2 and real_tg.sent()[1], 30)
        data = s2["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        mid = real_tg._message_id  # последний выданный id: сообщение со вторым вопросом
        real_tg.push({"callback_query": {"id": "go", "data": data, "from": {"id": CHAT},
                                         "message": {"message_id": mid, "chat": {"id": CHAT}}}})

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    prepared = launch.prepare(app, "tg-ask")
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    run_dir = app.paths.run_dir(rec.id)
    t.join(5)
    assert rec.label == "ok", (run_dir / "stderr.log").read_text(encoding="utf-8")
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "код 6 знаков, подтверждение True" in events and "482913" not in events
    assert "Telegram:" not in events  # ошибок отправки не было
    deleted = real_tg.sent("deleteMessage")
    assert len(deleted) == 1 and deleted[0]["chat_id"] == CHAT
    outward = [(m, p) for m, p in real_tg.calls if m != "deleteMessage"]
    assert "482913" not in json.dumps(outward, ensure_ascii=False)
    assert ("answerCallbackQuery", {"callback_query_id": "go", "text": tg_asks.T_ACCEPTED}) in real_tg.calls
    edits = [p["text"] for p in real_tg.sent("editMessageText")]
    assert any(e.endswith("Ответ принят") for e in edits) and any(e.endswith("Ответ принят: да") for e in edits)
    assert tg_asks.load_links(home) == [] and telegram.last_error(app) is None


ONE_QUESTION = SCRIPT.split("import dawg_replay_kit as kit")[0] + """import dawg_replay_kit as kit

code = kit.ask("Код из SMS", secret=True, timeout_minutes=1)
kit.result("ok", "готово")
"""


def read_events(run_dir) -> str:
    try:
        return (run_dir / "events.jsonl").read_text(encoding="utf-8")
    except OSError:
        return ""


def answer_when(run_dir, pred, timeout=30.0):
    """Отвечает на вопрос запуска, когда pred() истинно: скрипт не ждёт срока, тест не зависит от скорости."""

    def go():
        wait_for(lambda: pred() and asks.waiting(run_dir), timeout)
        asks.write_answer(run_dir, asks.waiting(run_dir)[0], "1234")

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def test_send_error_goes_to_journal_and_settings(cli, home, app, tmp_path, real_tg, fast, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    real_tg.fail["sendMessage"] = (403, "Forbidden: bot was blocked by the user")
    f = tmp_path / "tg-ask.py"
    f.write_text(ONE_QUESTION, encoding="utf-8")
    cli("add", str(f))
    prepared = launch.prepare(app, "tg-ask")
    run_dir = app.paths.run_dir(prepared.run.id)
    line = "Telegram: не удалось отправить вопрос в Telegram: бот не может написать в чат"
    # Отвечаем, только когда ошибка уже в журнале: вопрос точно ждал, пока его отправляли.
    t = answer_when(run_dir, lambda: line in read_events(run_dir))
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    t.join(5)
    assert rec.status == "ok"  # Telegram на итог не влияет
    assert "бот не может написать" in telegram.last_error(app)["text"]


class GatedBot(FakeBot):
    """send_message ждёт ворот: отправка дольше, чем close готов ждать."""

    def __init__(self, server, gate, entered):
        super().__init__(server)
        self.gate, self.entered = gate, entered

    def send_message(self, chat_id, text, reply_markup=None):
        self.entered.set()
        self.gate.wait(30)
        return super().send_message(chat_id, text, reply_markup)


def test_error_after_close_still_reaches_journal_and_settings(cli, home, app, tmp_path, server, fast, monkeypatch):
    """Медленная отправка не уложилась в close(wait): её ошибка всё равно в журнале и в Настройках."""
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    monkeypatch.setattr(tg_asks, "CLOSE_WAIT", 0.0)
    gate, entered = threading.Event(), threading.Event()
    server.fail_send = True
    monkeypatch.setattr(tg_asks, "open_bot", lambda a: (GatedBot(server, gate, entered), CHAT))
    f = tmp_path / "tg-ask.py"
    f.write_text(ONE_QUESTION, encoding="utf-8")
    cli("add", str(f))
    prepared = launch.prepare(app, "tg-ask")
    run_dir = app.paths.run_dir(prepared.run.id)
    t = answer_when(run_dir, entered.is_set)
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    t.join(5)
    assert rec.status == "ok"
    events = run_dir / "events.jsonl"
    assert "Telegram:" not in events.read_text(encoding="utf-8")  # supervise вернулся раньше ошибки
    gate.set()
    wait_for(lambda: "Telegram: не удалось отправить вопрос в Telegram" in events.read_text(encoding="utf-8"))
    wait_for(lambda: telegram.last_error(app))
    assert "bot was blocked" in telegram.last_error(app)["text"]


def test_close_late_errors_go_to_sink(tmp_path, server, fast):
    gate, entered = threading.Event(), threading.Event()
    server.fail_send = True
    ra = tg_asks.RunAsks(tmp_path, "r1", GatedBot(server, gate, entered), CHAT, title="T")
    write_events(tmp_path / "runs" / "r1", ask_event("a1"))
    ra.tick()
    entered.wait(5)
    got = []
    assert ra.close(wait=0.05, late=got.extend) == []
    gate.set()
    wait_for(lambda: got)
    assert got[0].startswith("не удалось отправить вопрос в Telegram")
    assert ra.take_errors() == []  # ничего не застряло в списке, который никто не заберёт


def test_telegram_not_connected_means_no_questions(app):
    assert tg_asks.open_bot(app) is None


# --- привязка чата делит getUpdates с опросом ответов ------------------------------------------


def test_pairing_waits_for_lock_and_shares_offset(app, kr, real_tg, monkeypatch):
    from fake_telegram import TOKEN

    monkeypatch.setattr(telegram._secrets, "token_urlsafe", lambda n: "fixedcode123")
    pairing = telegram.start_pairing(app, TOKEN)
    holder = tg_updates.UpdatesLock(app)
    assert holder.acquire(0)  # опрос ответов держит getUpdates
    real_tg.push_message("/start fixedcode123", chat_id=CHAT)
    got = {}
    t = threading.Thread(target=lambda: got.update(r=telegram.wait_pairing(app, pairing, seconds=10, poll=1)))
    t.start()
    time.sleep(0.5)
    assert real_tg.polls == 0  # привязка не зовёт getUpdates, пока блокировка занята
    holder.release()
    t.join(15)
    assert got["r"] == {"chat_id": str(CHAT), "chat_name": "Иван (@ivan)"}
    assert tg_updates.load_offset(app) == real_tg._update_id + 1  # общий offset сдвинут
    assert tg_updates.UpdatesLock(app).acquire(0)  # и отпущена


def test_pairing_does_not_answer_questions(app, kr, real_tg, tmp_path, monkeypatch):
    """Ответ, пришедший во время привязки, она не обрабатывает (описано в tg_updates)."""
    from fake_telegram import TOKEN

    monkeypatch.setattr(telegram._secrets, "token_urlsafe", lambda n: "fixedcode123")
    pairing = telegram.start_pairing(app, TOKEN)
    real_tg.push_message("какой-то ответ", chat_id=CHAT)
    real_tg.push_message("/start fixedcode123", chat_id=CHAT)
    assert telegram.wait_pairing(app, pairing, seconds=5, poll=1)["chat_id"] == str(CHAT)
    assert [m for m, _ in real_tg.calls if m != "getMe"] == ["sendMessage"]  # только «Подключено»
