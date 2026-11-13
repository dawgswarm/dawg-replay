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


LATER = NOW + timedelta(hours=1)


def insure(*args, **kw):
    """Юнит-тесты живут в часах NOW: к LATER любой POST давно закончился, ожидания нет."""
    kw.setdefault("now", lambda: LATER)
    kw.setdefault("wait", lambda s: pytest.fail(f"неожиданное ожидание {s} с"))
    return insurance.insure(*args, **kw)


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

    report = insure(tmp_path, factory)
    assert report.clean and report.released == []
    assert not (tmp_path / "insurance.json").exists()
    assert read_log(tmp_path) == []


# --- освобождение ---


def test_releases_open_without_close(tmp_path):
    write_events(tmp_path, [ev("provisioning"), ev("open", "a"), ev("provisioning"), ev("open", "b"), ev("close", "b")])
    api = FakeApi()
    report = insure(tmp_path, lambda: api)
    assert api.released == ["a"] and api.closed
    assert report.released == ["a"] and report.clean
    log = [e for e in read_log(tmp_path) if e["t"] == "log"]
    assert log[0]["level"] == "info" and "освобождён браузер a" in log[0]["msg"]
    saved = json.loads((tmp_path / "insurance.json").read_text(encoding="utf-8"))
    assert saved["released"] == ["a"]


def test_release_not_confirmed(tmp_path):
    write_events(tmp_path, [ev("open", "a")])
    report = insure(tmp_path, lambda: FakeApi(release_ok=False))
    assert report.failed == [{"session_id": "a", "error": "сессия осталась в списке ключа"}]
    assert read_log(tmp_path)[0]["level"] == "error"


def test_release_error(tmp_path):
    write_events(tmp_path, [ev("open", "a")])
    report = insure(tmp_path, lambda: FakeApi(fail="release"))
    assert report.failed[0]["error"] == "сеть" and not report.clean


def test_no_api_key(tmp_path):
    write_events(tmp_path, [ev("open", "a")])
    report = insure(tmp_path, None)
    assert report.error and report.failed[0]["session_id"] == "a"
    assert "ключа DAWG нет" in read_log(tmp_path)[0]["msg"]


def test_factory_error(tmp_path):
    write_events(tmp_path, [ev("open", "a")])

    def factory():
        raise RuntimeError("нет SDK")

    report = insure(tmp_path, factory)
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
    report = insure(tmp_path, lambda: api, finished_at=iso(NOW + timedelta(seconds=65)))
    assert [s["session_id"] for s in report.suspects] == ["in-window"]
    assert report.released == ["ours"]
    assert api.released == ["ours"]  # подозрительные сами не освобождаются
    warn = [e for e in read_log(tmp_path) if e.get("level") == "warn"]
    assert "возможно, остался браузер" in warn[0]["msg"] and "in-window" in warn[0]["msg"]


def test_pending_without_new_sessions(tmp_path):
    write_events(tmp_path, [ev("provisioning")])
    report = insure(tmp_path, lambda: FakeApi())
    assert report.suspects == [] and report.clean
    assert "новых сессий ключа не найдено" in read_log(tmp_path)[0]["msg"]


def test_pending_list_error(tmp_path):
    write_events(tmp_path, [ev("provisioning")])
    report = insure(tmp_path, lambda: FakeApi(fail="list"))
    assert "список сессий" in report.error


def test_second_pass_is_noop(tmp_path):
    write_events(tmp_path, [ev("open", "a", bid="b-a")])
    insure(tmp_path, lambda: FakeApi())
    closes = [e for e in read_log(tmp_path) if e["t"] == "session"]
    assert closes == [{"t": "session", "ts": closes[0]["ts"], "action": "close", "session_id": "a", "browser_id": "b-a", "source": "dawg-replay"}]

    def factory():
        raise AssertionError("второй проход не должен звать API")

    report = insure(tmp_path, factory)
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


def start_run(tmp_path, fake, body=SCRIPT, insure=True, **kw):
    path = tmp_path / "script.py"
    path.write_text(MANIFEST + textwrap.dedent(body), encoding="utf-8")
    run = ScriptRun(
        path,
        run_id="r1",
        runs_dir=tmp_path / "runs",
        command=[sys.executable],
        api_key=KEY,
        base_url=fake.url,
        after_exit=[insurance.hook(KEY, fake.url)] if insure else [],
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
def test_kill9_during_post_warns(tmp_path, fake, monkeypatch):
    fake.post_delay = 3  # сессия уже создана, ответ ещё не пришёл
    # Ожидание окончания POST укорочено под фейк: POST тут длится 3 с, а не до 30.
    monkeypatch.setattr(insurance, "POST_TIMEOUT", timedelta(seconds=3))
    monkeypatch.setattr(insurance, "POST_MARGIN", timedelta(seconds=1))
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
def test_without_hook_browser_leaks(tmp_path, fake):
    fake.ready_delay = 999
    run = start_run(tmp_path, fake, insure=False)
    assert wait_for(lambda: has(run, lambda e: e.get("action") == "open"))
    os.kill(run.pid, signal.SIGKILL)
    run.wait(30)
    assert len(fake.sessions) == 1
    assert not (run.run_dir / "insurance.json").exists()


# --- запуски без супервизора: lost и отмена (launch._close_lost) ---


def lost_run(app, fake):
    """Запись `running` без живого супервизора, в events.jsonl браузер с open без close."""
    import httpx

    from dawg_replay.runs import new_run_id

    resp = httpx.post(f"{fake.url}/api/v1/browsers", headers={"X-API-Key": KEY}).json()["data"]
    run_id = new_run_id()
    app.runs.reserve(
        run_id=run_id, script_name="x", script_version=1, script_path="/nope/script.py", draft=False,
        preset=None, params={}, timeout_seconds=60, limit=100,
    )
    app.db.execute("UPDATE runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?", (run_id,))
    write_events(
        app.paths.run_dir(run_id),
        [ev("provisioning"), ev("open", resp["sessionId"], bid=resp["browserId"])],
    )
    return run_id, resp["sessionId"]


@pytest.fixture
def app_with_key(app, fake):
    app.secrets.set_api_key(KEY)
    app.set_config("base_url", fake.url)
    return app


def test_lost_run_releases_browser(app_with_key, fake):
    from dawg_replay import launch

    run_id, sid = lost_run(app_with_key, fake)
    assert sid in fake.sessions
    lost = launch.recover_lost(app_with_key)
    assert [r.label for r in lost] == ["failed (lost)"]
    assert fake.sessions == {}
    run_dir = app_with_key.paths.run_dir(run_id)
    assert json.loads((run_dir / "insurance.json").read_text(encoding="utf-8"))["released"] == [sid]
    assert any(e.get("action") == "close" and e.get("session_id") == sid for e in read_log(run_dir))
    assert app_with_key.runs.get(run_id).error == "супервизор запуска пропал"  # хук отработал без ошибок


def test_cancel_without_supervisor_releases_browser(app_with_key, fake):
    from dawg_replay import launch

    run_id, sid = lost_run(app_with_key, fake)
    launch.cancel(app_with_key, run_id)
    assert app_with_key.runs.get(run_id).label == "failed (cancelled)"
    assert fake.sessions == {}


def test_lost_run_without_key_reports(app, fake):
    from dawg_replay import launch

    run_id, sid = lost_run(app, fake)
    launch.recover_lost(app)
    assert sid in fake.sessions  # ключа нет: освобождать нечем, но причина записана
    log = read_log(app.paths.run_dir(run_id))
    assert "ключа DAWG нет" in log[0]["msg"]


def test_lost_run_without_browsers_does_not_touch_keyring(app, monkeypatch):
    from dawg_replay import launch
    from dawg_replay.runs import new_run_id

    def boom():
        raise AssertionError("keyring не нужен")

    monkeypatch.setattr(app.secrets, "get_api_key", boom)
    run_id = new_run_id()
    app.runs.reserve(
        run_id=run_id, script_name="x", script_version=1, script_path="/nope/script.py", draft=False,
        preset=None, params={}, timeout_seconds=60, limit=100,
    )
    app.db.execute("UPDATE runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?", (run_id,))
    write_events(app.paths.run_dir(run_id), [{"t": "log", "level": "info", "msg": "без браузеров"}])
    assert [r.label for r in launch.recover_lost(app)] == ["failed (lost)"]
    assert app.runs.get(run_id).error == "супервизор запуска пропал"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_supervised_run_kill9_released_by_supervisor(app_with_key, fake, tmp_path, monkeypatch, kr):
    import threading

    from dawg_replay import launch
    from dawg_replay.app import App

    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    src = tmp_path / "script.py"
    src.write_text(MANIFEST + textwrap.dedent(SCRIPT), encoding="utf-8")
    prepared = launch.prepare(app_with_key, str(src))
    run_id = prepared.run.id
    done = {}
    def supervisor():
        # Как настоящий супервизор: своё подключение к БД в своём потоке.
        with App(app_with_key.paths.home, keyring_backend=kr) as own:
            done["rec"] = launch.supervise(own, run_id, prepared.payload())

    t = threading.Thread(target=supervisor)
    t.start()
    events_path = app_with_key.paths.run_dir(run_id) / "events.jsonl"

    def working():
        try:
            return "работаю" in events_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False

    assert wait_for(working)
    assert len(fake.sessions) == 1
    os.kill(app_with_key.runs.get(run_id).pid, signal.SIGKILL)
    t.join(30)
    assert done["rec"].status == "failed" and done["rec"].error is None
    assert fake.sessions == {}


# --- ожидание окончания POST перед поиском suspects (provisioning без open) -------


class Clock:
    """Подменные часы: ожидание не спит, а двигает время и даёт тесту что-то сделать «пока ждём»."""

    def __init__(self, now, during_wait=None, interrupt_after=None):
        self.now = now
        self.waits = []
        self.during_wait = during_wait
        self.interrupt_after = interrupt_after

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.during_wait:
            self.during_wait(self)
        if self.interrupt_after is not None and self.interrupt_after < seconds:
            self.now += timedelta(seconds=self.interrupt_after)
            return False
        self.now += timedelta(seconds=seconds)
        return True


def fake_api(fake):
    return lambda: Sessions(KEY, fake.url)


def test_post_wait_seconds():
    t = NOW
    assert insurance.post_wait_seconds([iso(t)], t) == 35
    assert insurance.post_wait_seconds([iso(t)], t + timedelta(seconds=3)) == 32
    assert insurance.post_wait_seconds([iso(t)], t + timedelta(seconds=40)) == 0
    # Последний provisioning решает.
    assert insurance.post_wait_seconds([iso(t), iso(t + timedelta(seconds=10))], t + timedelta(seconds=20)) == 25
    # ts из будущего (часы скрипта убежали): не дольше 35 с от текущего момента.
    assert insurance.post_wait_seconds([iso(t + timedelta(hours=1))], t) == 35
    assert insurance.post_wait_seconds(["битое"], t) == 0


def test_waits_for_post_and_finds_browser_finished_after_death(tmp_path, fake):
    """Процесс умер через 3 с после provisioning; платформа доделала браузер позже."""
    prov = NOW
    write_events(tmp_path, [ev("provisioning", ts=prov)])
    late = {}

    def platform_finishes(clock):
        # startedAt — момент приёма POST, а в списке ключа браузер появляется, когда платформа его доделала.
        late["sid"] = fake.add_foreign_session(iso(prov + timedelta(seconds=1)))

    clock = Clock(prov + timedelta(seconds=3), during_wait=platform_finishes)
    report = insurance.insure(tmp_path, fake_api(fake), finished_at=iso(prov + timedelta(seconds=3)), now=clock, wait=clock.wait)
    assert clock.waits == [32]  # до provisioning + 30 + 5
    assert report.waited_s == 32 and not report.wait_interrupted
    assert [s["session_id"] for s in report.suspects] == [late["sid"]]
    assert late["sid"] in fake.sessions  # подозрительные сами не освобождаются


def test_without_wait_late_browser_would_be_missed(tmp_path, fake):
    """Контроль: если бы список брали сразу, доделанного платформой браузера в нём ещё не было бы."""
    write_events(tmp_path, [ev("provisioning", ts=NOW)])
    clock = Clock(NOW + timedelta(seconds=3))
    report = insurance.insure(tmp_path, fake_api(fake), now=clock, wait=lambda s: True)
    assert report.suspects == []


def test_no_wait_when_post_long_over(tmp_path, fake):
    write_events(tmp_path, [ev("provisioning", ts=NOW)])
    clock = Clock(NOW + timedelta(minutes=5))
    insurance.insure(tmp_path, fake_api(fake), now=clock, wait=clock.wait)
    assert clock.waits == []


def test_no_wait_without_pending_provisioning(tmp_path, fake):
    # open без close: освобождаем сразу, ничего не ждём.
    sid = fake.add_foreign_session(iso(NOW))
    write_events(tmp_path, [ev("provisioning", ts=NOW), ev("open", sid, ts=NOW)])
    clock = Clock(NOW + timedelta(seconds=1))
    report = insurance.insure(tmp_path, fake_api(fake), now=clock, wait=clock.wait)
    assert clock.waits == [] and report.released == [sid] and fake.sessions == {}
    # failed закрывает provisioning: тоже без ожидания.
    write_events(tmp_path / "f", [ev("provisioning", ts=NOW), ev("failed", ts=NOW)])
    insurance.insure(tmp_path / "f", fake_api(fake), now=clock, wait=clock.wait)
    assert clock.waits == []


def test_unclosed_released_before_waiting(tmp_path, fake):
    sid = fake.add_foreign_session(iso(NOW))
    write_events(tmp_path, [ev("provisioning", ts=NOW), ev("open", sid, ts=NOW), ev("provisioning", ts=NOW + timedelta(seconds=2))])
    seen = []
    clock = Clock(NOW + timedelta(seconds=3), during_wait=lambda c: seen.append(dict(fake.sessions)))
    report = insurance.insure(tmp_path, fake_api(fake), now=clock, wait=clock.wait)
    assert seen == [{}]  # к началу ожидания браузер запуска уже освобождён
    assert report.released == [sid] and clock.waits == [34]


def test_interrupted_wait_keeps_report(tmp_path, fake):
    write_events(tmp_path, [ev("provisioning", ts=NOW)])
    early = fake.add_foreign_session(iso(NOW + timedelta(seconds=1)))
    clock = Clock(NOW + timedelta(seconds=3), interrupt_after=2)
    report = insurance.insure(tmp_path, fake_api(fake), now=clock, wait=clock.wait)
    assert report.wait_interrupted and report.waited_s == 2
    assert [s["session_id"] for s in report.suspects] == [early]  # итог всё равно посчитан
    saved = json.loads((tmp_path / "insurance.json").read_text(encoding="utf-8"))
    assert saved["wait_interrupted"] is True and saved["suspects"][0]["session_id"] == early
    msgs = [e["msg"] for e in read_log(tmp_path)]
    assert any("ожидание окончания создания браузера прервано" in m for m in msgs)
    assert any("возможно, остался браузер" in m for m in msgs)


def test_event_wait_interrupts_promptly():
    import threading

    stop = threading.Event()
    wait = insurance.event_wait(stop)
    threading.Timer(0.1, stop.set).start()
    t0 = time.monotonic()
    assert wait(30) is False
    assert time.monotonic() - t0 < 2
    assert insurance.event_wait(None)(0.01) is True


def test_hook_passes_stop_to_wait(tmp_path, fake):
    import threading
    from types import SimpleNamespace

    write_events(tmp_path, [ev("provisioning", ts=iso_now_minus(1))])  # POST идёт прямо сейчас
    stop = threading.Event()
    stop.set()  # приложение уже выходит: ждать нельзя
    t0 = time.monotonic()
    insurance.hook(KEY, fake.url, stop=stop)(SimpleNamespace(run_dir=tmp_path, finished_at=None))
    assert time.monotonic() - t0 < 5
    report = json.loads((tmp_path / "insurance.json").read_text(encoding="utf-8"))
    assert report["wait_interrupted"] is True


def iso_now_minus(seconds):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_supervised_cancel_interrupts_post_wait(app_with_key, fake, tmp_path, monkeypatch, kr):
    """kill -9 во время POST: страховка ждёт окончания POST; cancel из другого процесса
    прерывает ожидание, итог запуска всё равно записан."""
    import threading

    from dawg_replay import launch
    from dawg_replay.app import App

    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    monkeypatch.setattr(insurance, "POST_TIMEOUT", timedelta(seconds=120))  # долгое ожидание
    fake.post_delay = 3
    src = tmp_path / "script.py"
    src.write_text(MANIFEST + textwrap.dedent(SCRIPT), encoding="utf-8")
    prepared = launch.prepare(app_with_key, str(src))
    run_id = prepared.run.id
    done = {}

    def supervisor():
        with App(app_with_key.paths.home, keyring_backend=kr) as own:
            done["rec"] = launch.supervise(own, run_id, prepared.payload())

    t = threading.Thread(target=supervisor)
    t.start()
    run_dir = app_with_key.paths.run_dir(run_id)
    assert wait_for(lambda: '"provisioning"' in (run_dir / "events.jsonl").read_text(encoding="utf-8") if (run_dir / "events.jsonl").exists() else False)
    assert wait_for(lambda: len(fake.sessions) == 1)  # POST принят, ответа ещё нет
    os.kill(app_with_key.runs.get(run_id).pid, signal.SIGKILL)
    time.sleep(1)
    assert t.is_alive()  # страховка ждёт окончания POST
    t0 = time.monotonic()
    app_with_key.runs.request_cancel(run_id)
    t.join(15)
    assert not t.is_alive() and time.monotonic() - t0 < 5
    assert done["rec"].status == "failed"
    report = json.loads((run_dir / "insurance.json").read_text(encoding="utf-8"))
    assert report["wait_interrupted"] is True
    assert len(report["suspects"]) == 1  # браузер из прерванного POST всё равно найден
