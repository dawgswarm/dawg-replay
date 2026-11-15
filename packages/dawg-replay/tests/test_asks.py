"""Вопрос посреди запуска: dawg_replay.asks, экран запуска, CLI answer и ввод при run --follow, сквозной запуск с kit.ask."""

import io
import json
import os
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from dawg_replay import asks
from dawg_replay.app import App
from dawg_replay.cli_run import TerminalAnswers
from dawg_replay.errors import ReplayError
from dawg_replay.runner import ScriptRun
from dawg_replay.runview import RunView
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54351
BASE = f"http://127.0.0.1:{PORT}"
RUN_ID = "20261115-120000-aaaaaa"
POSIX = sys.platform != "win32"


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def ask_event(ask_id="1-abc", question="Код из SMS", kind="text", secret=False, choices=None, minutes=10):
    e = {"t": "ask", "ts": iso(datetime.now(timezone.utc)), "id": ask_id, "question": question, "kind": kind,
         "secret": secret, "deadline": iso(datetime.now(timezone.utc) + timedelta(minutes=minutes))}
    if choices:
        e["choices"] = choices
    return e


def write_events(run_dir, *events):
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "events.jsonl", "a", encoding="utf-8", newline="") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


@pytest.fixture
def running(home):
    """Идущий запуск в БД с вопросами в событиях."""

    def make(*events, run_id=RUN_ID):
        with App(home) as app:
            app.runs.reserve(run_id=run_id, script_name="ozon-cards", script_version=None, script_path="/x/script.py",
                             draft=True, preset=None, params={}, timeout_seconds=600, limit=100)
            run_dir = app.paths.run_dir(run_id)
        write_events(run_dir, *events)
        return run_dir

    return make


# --- dawg_replay.asks -----------------------------------------------------------


def test_waiting_questions_and_closing(tmp_path):
    run_dir = tmp_path / "run"
    write_events(run_dir, ask_event("1-a"), ask_event("2-b", kind="confirm"), {"t": "log", "msg": "ask"},
                 "битая", ask_event("3-c", minutes=-1))
    assert [q.id for q in asks.waiting(run_dir)] == ["1-a", "2-b"]  # у 3-c срок вышел
    write_events(run_dir, {"t": "ask_closed", "id": "1-a", "outcome": "answered", "answer": "42"})
    (q2,) = asks.waiting(run_dir)
    assert q2.id == "2-b"
    asks.write_answer(run_dir, q2, True)
    assert asks.waiting(run_dir) == []  # ответ лежит, скрипт его ещё не забрал
    closed = asks.read(run_dir / "events.jsonl")[0]
    assert (closed.outcome, closed.answer) == ("answered", "42")


def test_write_answer_atomic_private_and_once(tmp_path):
    q = asks.parse_ask(ask_event("1-a", secret=True))
    path = asks.write_answer(tmp_path, q, "482913")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["id"], data["answer"], data["secret"]) == ("1-a", "482913", True)
    assert [p.name for p in path.parent.iterdir()] == ["1-a.json"]  # без временных файлов
    if POSIX:
        assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ReplayError) as e:
        asks.write_answer(tmp_path, q, "000000")
    assert e.value.code == "already_answered"
    with pytest.raises(ReplayError):
        asks.answer_path(tmp_path, "../x")


def test_normalize():
    confirm = asks.parse_ask(ask_event(kind="confirm"))
    assert asks.normalize(confirm, "yes") is True and asks.normalize(confirm, "нет") is False
    choice = asks.parse_ask(ask_event(kind="choice", choices=["Москва", "Казань"]))
    assert asks.normalize(choice, "2") == "Казань" and asks.normalize(choice, "Москва") == "Москва"
    text = asks.parse_ask(ask_event())
    assert asks.normalize(text, "  12 34 ") == "12 34"
    for q, raw in ((confirm, "может"), (choice, "Тула"), (choice, "3"), (text, "  ")):
        with pytest.raises(ReplayError) as e:
            asks.normalize(q, raw)
        assert e.value.code == "bad_answer"


def test_pick_by_id_and_several(tmp_path):
    write_events(tmp_path, ask_event("1-a"), ask_event("2-b"))
    with pytest.raises(ReplayError) as e:
        asks.pick(tmp_path)
    assert e.value.code == "several_questions" and "1-a" in e.value.message
    assert asks.pick(tmp_path, "2-b").id == "2-b"
    with pytest.raises(ReplayError) as e:
        asks.pick(tmp_path, "9-z")
    assert e.value.code == "no_question"


def test_drop_secret_answers(tmp_path):
    secret = asks.parse_ask(ask_event("1-a", secret=True))
    plain = asks.parse_ask(ask_event("2-b"))
    asks.write_answer(tmp_path, secret, "482913")
    asks.write_answer(tmp_path, plain, "Казань")
    (tmp_path / "answers" / ".3-c.ab.tmp").write_text("{}", encoding="utf-8")
    asks.drop_secret_answers(tmp_path)
    assert sorted(p.name for p in (tmp_path / "answers").iterdir()) == ["2-b.json"]
    asks.drop_secret_answers(tmp_path / "нет")  # каталога нет: без ошибок


def test_runview_question_and_log(tmp_path):
    run_dir = tmp_path / "run"
    write_events(run_dir, ask_event("1-a", question="Склад?", kind="choice", choices=["Москва", "Казань"]))
    view = RunView(run_dir)
    snap = view.snapshot()
    assert snap["question"]["id"] == "1-a" and snap["question"]["choices"] == ["Москва", "Казань"]
    assert snap["log"][-1]["msg"] == "вопрос: Склад?"
    write_events(run_dir, {"t": "ask_closed", "id": "1-a", "outcome": "answered", "answer": "Казань"},
                 ask_event("2-b", secret=True), {"t": "ask_closed", "id": "2-b", "outcome": "answered"},
                 ask_event("3-c"), {"t": "ask_closed", "id": "3-c", "outcome": "timeout"})
    snap = view.snapshot()
    assert snap["question"] is None
    msgs = [e["msg"] for e in snap["log"]]
    assert "ответ получен: Казань" in msgs and "ответ получен" in msgs
    assert "не дождались ответа на вопрос «Код из SMS»" in msgs
    write_events(run_dir, {"t": "ask_closed", "id": "нет", "outcome": "answered"})
    assert view.snapshot()["log"][-1]["level"] == "raw"


# --- веб ------------------------------------------------------------------------


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def client(home, kr, secrets_):
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c


def post(c, secrets_, url, data):
    return c.post(url, data=data, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf})


def test_web_question_block_answer_and_marks(client, secrets_, running, home):
    run_dir = running(ask_event("1-a", question="Код из SMS на номер ***-12", secret=True))
    h = client.get(f"/runs/{RUN_ID}").text
    assert "Скрипт ждёт ответа" in h and "Код из SMS на номер ***-12" in h and 'type="password"' in h
    assert "ждёт ответа" in h and f"dawg-replay answer {RUN_ID}" in h
    live = client.get("/home/_live").text
    assert "ждёт ответа" in live and f'data-ask-id="{RUN_ID}/1-a"' in live
    assert client.get("/events/asks").json() == {"questions": [
        {"id": f"{RUN_ID}/1-a", "run": RUN_ID, "title": "ozon-cards", "question": "Код из SMS на номер ***-12"}]}

    r = post(client, secrets_, f"/runs/{RUN_ID}/answer", {"ask_id": "1-a", "answer": " 482913 "})
    assert r.status_code == 200 and "Ответ отправлен" in r.text and "482913" not in r.text
    assert json.loads((run_dir / "answers" / "1-a.json").read_text(encoding="utf-8"))["answer"] == "482913"
    assert client.get("/events/asks").json() == {"questions": []}
    r = post(client, secrets_, f"/runs/{RUN_ID}/answer", {"ask_id": "1-a", "answer": "1"})
    assert "уже ответили" in r.text
    # Ответ не попадает ни в БД, ни в файлы запуска, кроме файла ответа (его удалит скрипт).
    with App(home) as app:
        assert "482913" not in json.dumps(app.runs.get(RUN_ID).to_dict(), ensure_ascii=False)
    assert "482913" not in (run_dir / "events.jsonl").read_text(encoding="utf-8")


def test_web_confirm_buttons_and_bad_answer(client, secrets_, running):
    running(ask_event("1-a", question="Отправить 12 заявок?", kind="confirm"))
    h = client.get(f"/runs/{RUN_ID}").text
    assert 'name="answer" value="yes"' in h and 'name="answer" value="no"' in h
    r = post(client, secrets_, f"/runs/{RUN_ID}/answer", {"ask_id": "1-a", "answer": "может"})
    assert "yes или no" in r.text and "Отправить 12 заявок?" in r.text  # вопрос остаётся на экране
    r = post(client, secrets_, f"/runs/{RUN_ID}/answer", {"ask_id": "1-a", "answer": "no"})
    assert "Ответ «нет» отправлен" in r.text
    assert c_headers_ok(client, secrets_)


def c_headers_ok(client, secrets_):
    # Без CSRF ответ не принимается.
    r = client.post(f"/runs/{RUN_ID}/answer", data={"answer": "yes"}, headers={"Origin": BASE})
    return r.status_code == 403


def test_web_no_question_and_finished_run(client, secrets_, running, home):
    running()
    r = post(client, secrets_, f"/runs/{RUN_ID}/answer", {"answer": "yes"})
    assert "ничего не спрашивает" in r.text
    with App(home) as app:
        app.runs.finish(RUN_ID, status="ok", reason=None)
    write_events(home / "runs" / RUN_ID, ask_event("1-a"))
    assert "Скрипт ждёт ответа" not in client.get(f"/runs/{RUN_ID}").text
    assert "уже завершён" in post(client, secrets_, f"/runs/{RUN_ID}/answer", {"answer": "x"}).text


def test_script_runs_list_mark(client, running, home, write_script):
    with App(home) as app:
        app.registry.add(write_script(version=1))
    running(ask_event("1-a"))
    with App(home) as app:
        app.runs._db.execute("UPDATE runs SET draft = 0 WHERE id = ?", (RUN_ID,))
    h = client.get("/scripts/ozon-cards/_runs").text
    assert "status-ask" in h and "every 5s" in h


# --- CLI ------------------------------------------------------------------------


def test_cli_answer(cli, running):
    run_dir = running(ask_event("1-a", question="Какой склад?", kind="choice", choices=["Москва", "Казань"]))
    code, data = cli("answer", RUN_ID, "Тула")
    assert code != 0 and data["error"]["code"] == "bad_answer"
    code, data = cli("answer", RUN_ID, "2")
    assert code == 0 and data["accepted"] == "Казань" and data["question"]["id"] == "1-a"
    assert json.loads((run_dir / "answers" / "1-a.json").read_text(encoding="utf-8"))["answer"] == "Казань"
    code, data = cli("answer", RUN_ID, "1")
    assert code != 0 and data["error"]["code"] == "no_question"


def test_cli_answer_secret_from_stdin(cli, running):
    run_dir = running(ask_event("1-a", secret=True))
    code, data = cli("answer", RUN_ID, "-", stdin="482913\n")
    assert code == 0 and data["accepted"] is None and "482913" not in json.dumps(data)
    assert json.loads((run_dir / "answers" / "1-a.json").read_text(encoding="utf-8"))["answer"] == "482913"


def test_terminal_answers_during_follow(tmp_path):
    write_events(tmp_path, ask_event("1-a", question="Отправить?", kind="confirm"))
    out = io.StringIO()
    lines = iter(["может\n", "да\n"])
    ta = TerminalAnswers(tmp_path, out, read_line=lambda: next(lines), read_secret=lambda: "x")
    ta.poll()
    ta._reader.join(5)
    assert json.loads((tmp_path / "answers" / "1-a.json").read_text(encoding="utf-8"))["answer"] is True
    text = out.getvalue()
    assert "скрипт спрашивает: Отправить?" in text and "попробуйте ещё раз" in text and "ответ отправлен" in text
    write_events(tmp_path, ask_event("2-b", secret=True))
    secret_reads = []
    ta._read_secret = lambda: secret_reads.append(1) or "482913\n"
    ta.poll()
    ta._reader.join(5)
    assert secret_reads == [1] and "482913" not in out.getvalue()
    ta.poll()  # на те же вопросы второй раз не спрашивает
    assert ta._reader is None or not ta._reader.is_alive()


# --- сквозной запуск с kit -------------------------------------------------------

MANIFEST = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit"]
# ///

# /// dawg-script
# contract = 1
# name = "ask-test"
# title = "Вопрос"
# version = 1
# ///
'''


def script_run(tmp_path, body, **kw):
    f = tmp_path / "script.py"
    f.write_text(MANIFEST + "\n" + textwrap.dedent(body), encoding="utf-8")
    kw.setdefault("timeout", 60)
    return ScriptRun(f, "r1", tmp_path / "runs", command=[sys.executable], **kw)


def wait_question(run_dir, timeout=10.0, seen=()):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qs = asks.waiting(run_dir)
        assert not [q for q in qs if q.id in seen], "отвеченный вопрос снова выглядит открытым"
        if qs:
            return qs[0]
        time.sleep(0.05)
    raise AssertionError("скрипт не задал вопрос")


def test_end_to_end_secret_and_confirm(tmp_path):
    run = script_run(tmp_path, '''
        import dawg_replay_kit as kit
        code = kit.ask("Код из SMS", secret=True, timeout_minutes=1)
        ok = kit.ask("Отправить?", kind="confirm")
        kit.log.info(f"код {code}, подтверждение {ok}")
        kit.result("ok", "готово")
    ''')
    run.start()
    q = wait_question(run.run_dir)
    assert (q.question, q.secret) == ("Код из SMS", True)
    path = asks.write_answer(run.run_dir, q, "482913")
    q2 = wait_question(run.run_dir, seen={q.id})
    assert not path.exists()  # секрет kit удалил сразу после чтения
    asks.write_answer(run.run_dir, q2, True)
    outcome = run.wait(30)
    assert outcome is not None and outcome.status == "ok", (run.stderr_path.read_text(encoding="utf-8"))
    events = run.events_path.read_text(encoding="utf-8")
    assert "482913" not in events and "код ***, подтверждение True" in events
    closed = [json.loads(line) for line in events.splitlines() if '"ask_closed"' in line]
    assert [c.get("answer") for c in closed] == [None, True]
    assert (run.answers_dir / f"{q2.id}.json").exists()  # несекретный ответ остаётся


def test_end_to_end_timeout_fails_with_clear_error(tmp_path):
    run = script_run(tmp_path, '''
        import dawg_replay_kit as kit
        kit.ask("Код?", timeout_minutes=0.01)
    ''')
    outcome = run.run()
    assert outcome.status == "failed"
    assert "не дождались ответа на вопрос «Код?»" in run.stderr_path.read_text(encoding="utf-8")


def test_cancel_while_waiting_and_leftover_secret_removed(tmp_path):
    run = script_run(tmp_path, '''
        import time
        import dawg_replay_kit as kit
        kit.ask("Код", secret=True)
    ''', grace=10)
    run.start()
    q = wait_question(run.run_dir)
    # Ответ, который скрипт не успеет прочитать: после выхода его удалит приложение.
    stale = asks.parse_ask({**q.to_dict(), "t": "ask", "id": "9-zz"})
    asks.write_answer(run.run_dir, stale, "482913")
    t0 = time.monotonic()
    run.cancel()
    outcome = run.wait(15)
    assert outcome is not None and time.monotonic() - t0 < 5 and outcome.killed is False
    assert outcome.reason == "cancelled"
    assert not (run.answers_dir / "9-zz.json").exists()


def test_env_has_answers_and_logins_dirs_together(tmp_path):
    run = script_run(tmp_path, "", logins_dir=tmp_path / "logins")
    env = run._environment(run.params_path)
    assert env["DAWG_SCRIPT_ANSWERS_DIR"] == str((tmp_path / "runs" / "r1" / "answers").resolve())
    assert env["DAWG_SCRIPT_LOGINS_DIR"] == str((tmp_path / "logins").resolve())
