import json
import os
import signal
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone

import pytest
from fake_dawg import KEY, FakeDawg

from dawg_replay import insurance
from dawg_replay.runner import ScriptRun
from dawg_replay_kit.dawg_api import Sessions


@pytest.fixture
def fake():
    f = FakeDawg().start()
    yield f
    f.stop()


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)


def write_events(run_dir, events):
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "events.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")


def ev(action, sid=None, ts=NOW, bid=None):
    e = {"t": "session", "ts": iso(ts), "action": action}
    if sid:
        e["session_id"] = sid
    if bid:
        e["browser_id"] = bid
    return e


def read_log(run_dir):
    lines = [json.loads(x) for x in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if x.startswith("{")]
    return [e for e in lines if e.get("source") == "dawg-replay"]


class FakeApi:
    def __init__(self, sessions=(), release_ok=True, fail=None):
        self.sessions = list(sessions)
        self.released = []
        self.release_ok = release_ok
        self.fail = fail
        self.closed = False

    def list(self):
        if self.fail == "list":
            raise RuntimeError("сеть")
        return self.sessions

    def release(self, sid):
        if self.fail == "release":
            raise RuntimeError("сеть")
        self.released.append(sid)
        return self.release_ok

    def close(self):
        self.closed = True


# --- разбор событий ---


def test_scan_events(tmp_path):
    write_events(
        tmp_path,
        [
            ev("provisioning"),
            ev("open", "a", bid="b-a"),
            "битая строка",
            {"t": "log", "level": "info", "msg": "x"},
            ev("provisioning"),
            ev("open", "b"),
            ev("close", "a"),
            ev("provisioning"),
            ev("failed"),
            ev("provisioning", ts=NOW + timedelta(seconds=5)),
        ],
    )
    scan = insurance.scan_events(tmp_path / "events.jsonl")
    assert scan.unclosed == {"b": None}
    assert scan.known == {"a", "b"}
    assert scan.pending == [iso(NOW + timedelta(seconds=5))]


def test_nothing_to_do_does_not_touch_api(tmp_path):
    write_events(tmp_path, [ev("provisioning"), ev("open", "a"), ev("close", "a")])

    def factory():
        raise AssertionError("API не нужен")

    report = insurance.insure(tmp_path, factory)
    assert report.clean and report.released == []
    assert not (tmp_path / "insurance.json").exists()
    assert read_log(tmp_path) == []


# --- освобождение ---


def test_releases_open_without_close(tmp_path):
    write_events(tmp_path, [ev("provisioning"), ev("open", "a"), ev("provisioning"), ev("open", "b"), ev("close", "b")])
    api = FakeApi()
    report = insurance.insure(tmp_path, lambda: api)
    assert api.released == ["a"] and api.closed
    assert report.released == ["a"] and report.clean
    log = [e for e in read_log(tmp_path) if e["t"] == "log"]
    assert log[0]["level"] == "info" and "освобождён браузер a" in log[0]["msg"]
    saved = json.loads((tmp_path / "insurance.json").read_text(encoding="utf-8"))
    assert saved["released"] == ["a"]


def test_release_not_confirmed(tmp_path):
    write_events(tmp_path, [ev("open", "a")])
    report = insurance.insure(tmp_path, lambda: FakeApi(release_ok=False))
    assert report.failed == [{"session_id": "a", "error": "сессия осталась в списке ключа"}]
    assert read_log(tmp_path)[0]["level"] == "error"


def test_release_error(tmp_path):
    write_events(tmp_path, [ev("open", "a")])
    report = insurance.insure(tmp_path, lambda: FakeApi(fail="release"))
    assert report.failed[0]["error"] == "сеть" and not report.clean


def test_no_api_key(tmp_path):
    write_events(tmp_path, [ev("open", "a")])
    report = insurance.insure(tmp_path, None)
    assert report.error and report.failed[0]["session_id"] == "a"
    assert "ключа DAWG нет" in read_log(tmp_path)[0]["msg"]


def test_factory_error(tmp_path):
    write_events(tmp_path, [ev("open", "a")])

    def factory():
        raise RuntimeError("нет SDK")

    report = insurance.insure(tmp_path, factory)
    assert "нет SDK" in report.error and report.failed[0]["session_id"] == "a"


# --- остаточное окно ---


def session(sid, started, bid="x"):
    return {"id": sid, "browserInstanceId": bid, "status": "ACTIVE", "startedAt": iso(started)}


def test_pending_provisioning_lists_suspects(tmp_path):
    write_events(tmp_path, [ev("provisioning", ts=NOW), ev("open", "ours", ts=NOW + timedelta(seconds=1))] + [ev("provisioning", ts=NOW + timedelta(seconds=60))])
    api = FakeApi(
        sessions=[
            session("ours", NOW + timedelta(seconds=1)),  # своя, известна
            session("in-window", NOW + timedelta(seconds=62)),
            session("before", NOW + timedelta(seconds=30)),  # до окна
            session("long-ago", NOW - timedelta(hours=1)),
            session("after", NOW + timedelta(seconds=300)),  # после конца процесса
        ]
    )
    report = insurance.insure(tmp_path, lambda: api, finished_at=iso(NOW + timedelta(seconds=65)))
    assert [s["session_id"] for s in report.suspects] == ["in-window"]
    assert report.released == ["ours"]
    assert api.released == ["ours"]  # подозрительные сами не освобождаются
    warn = [e for e in read_log(tmp_path) if e.get("level") == "warn"]
    assert "возможно, остался браузер" in warn[0]["msg"] and "in-window" in warn[0]["msg"]


def test_pending_without_new_sessions(tmp_path):
    write_events(tmp_path, [ev("provisioning")])
    report = insurance.insure(tmp_path, lambda: FakeApi())
    assert report.suspects == [] and report.clean
    assert "новых сессий ключа не найдено" in read_log(tmp_path)[0]["msg"]


def test_pending_list_error(tmp_path):
    write_events(tmp_path, [ev("provisioning")])
    report = insurance.insure(tmp_path, lambda: FakeApi(fail="list"))
    assert "список сессий" in report.error


def test_second_pass_is_noop(tmp_path):
    write_events(tmp_path, [ev("open", "a", bid="b-a")])
    insurance.insure(tmp_path, lambda: FakeApi())
    closes = [e for e in read_log(tmp_path) if e["t"] == "session"]
    assert closes == [{"t": "session", "ts": closes[0]["ts"], "action": "close", "session_id": "a", "browser_id": "b-a", "source": "dawg-replay"}]

    def factory():
        raise AssertionError("второй проход не должен звать API")

    report = insurance.insure(tmp_path, factory)
    assert report.released == [] and report.clean
    assert len(read_log(tmp_path)) == 2  # close и одна строка лога


# --- сквозные проверки с раннером и фейковым API ---

MANIFEST = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

# /// dawg-script
# contract = 1
# name = "insurance-test"
# title = "Страховка"
# version = 1
# ///
'''

# Подключение Playwright подменяется: в фейке нет настоящего CDP.
SCRIPT = '''
import time
import dawg_replay_kit as kit
from dawg_replay_kit.browser import Connection

k = kit.init()
k.browsers.connect = lambda url: Connection("B", "C", "P", lambda: None)
kit.log.info("старт")
with kit.browser() as b:
    kit.log.info("работаю")
    time.sleep(60)
'''


def start_run(tmp_path, fake, body=SCRIPT, **kw):
    path = tmp_path / "script.py"
    path.write_text(MANIFEST + textwrap.dedent(body), encoding="utf-8")
    run = ScriptRun(
        path,
        run_id="r1",
        runs_dir=tmp_path / "runs",
        command=[sys.executable],
        api_key=KEY,
        base_url=fake.url,
        **kw,
    )
    run.start()
    return run


def run_events(run):
    return [json.loads(x) for x in run.events_path.read_text(encoding="utf-8").splitlines()]


def wait_for(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def has(run, cond):
    try:
        return any(cond(e) for e in run_events(run))
    except (FileNotFoundError, json.JSONDecodeError):
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_kill9_while_waiting_for_browser(tmp_path, fake):
    fake.ready_delay = 999
    run = start_run(tmp_path, fake)
    assert wait_for(lambda: has(run, lambda e: e.get("action") == "open"))
    assert len(fake.sessions) == 1
    os.kill(run.pid, signal.SIGKILL)
    outcome = run.wait(30)
    assert outcome.status == "failed" and outcome.hook_errors == []
    assert fake.sessions == {}
    report = json.loads((run.run_dir / "insurance.json").read_text(encoding="utf-8"))
    assert len(report["released"]) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_kill9_while_working(tmp_path, fake):
    run = start_run(tmp_path, fake)
    assert wait_for(lambda: has(run, lambda e: e.get("msg") == "работаю"))
    os.kill(run.pid, signal.SIGKILL)
    run.wait(30)
    assert fake.sessions == {}
    assert any("освобождён браузер" in e.get("msg", "") for e in run_events(run))


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_kill9_during_post_warns(tmp_path, fake):
    fake.post_delay = 3  # сессия уже создана, ответ ещё не пришёл
    foreign = fake.add_foreign_session(iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    run = start_run(tmp_path, fake)
    assert wait_for(lambda: has(run, lambda e: e.get("action") == "provisioning"))
    assert wait_for(lambda: len(fake.sessions) == 2)
    os.kill(run.pid, signal.SIGKILL)
    run.wait(30)
    report = json.loads((run.run_dir / "insurance.json").read_text(encoding="utf-8"))
    assert len(report["suspects"]) == 1 and report["suspects"][0]["session_id"] != foreign
    assert foreign in fake.sessions and len(fake.sessions) == 2  # ничего не освобождено без спроса
    assert any("возможно, остался браузер" in e.get("msg", "") for e in run_events(run))
    assert insurance.release_session(KEY, report["suspects"][0]["session_id"], fake.url)
    assert list(fake.sessions) == [foreign]


def test_soft_cancel_kit_releases_itself(tmp_path, fake):
    run = start_run(tmp_path, fake, grace=10)
    assert wait_for(lambda: has(run, lambda e: e.get("msg") == "работаю"))
    run.cancel()
    outcome = run.wait(30)
    assert outcome.reason == "cancelled" and not outcome.killed
    assert fake.sessions == {}
    acts = [e["action"] for e in run_events(run) if e["t"] == "session"]
    assert acts == ["provisioning", "open", "close"]
    assert not (run.run_dir / "insurance.json").exists()  # страховке делать нечего


def test_normal_exit_closes(tmp_path, fake):
    body = SCRIPT.replace("time.sleep(60)", "pass")
    run = start_run(tmp_path, fake, body=body)
    outcome = run.wait(30)
    assert outcome.status == "ok", (run.stderr_path.read_text(),)
    assert fake.sessions == {}
    assert KEY not in run.events_path.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_runner_can_skip_insurance(tmp_path, fake):
    fake.ready_delay = 999
    run = start_run(tmp_path, fake, insure=False)
    assert wait_for(lambda: has(run, lambda e: e.get("action") == "open"))
    os.kill(run.pid, signal.SIGKILL)
    run.wait(30)
    assert len(fake.sessions) == 1
    assert not (run.run_dir / "insurance.json").exists()
