import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from dawg_replay import launch, procinfo
from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from dawg_replay.runs import RunStore, WaitPolicy


def reserve(store, run_id, limit=1, wait=None, **kw):
    return store.reserve(
        run_id=run_id, script_name="s", script_version=1, script_path="x", draft=False, preset=None,
        params={}, timeout_seconds=60, limit=limit, wait=wait, **kw,
    )


class FakeTime:
    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.on_sleep = on_sleep
        self.sleeps = 0

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps += 1
        self.now += s
        if self.on_sleep:
            self.on_sleep(self)


@pytest.fixture
def store(app):
    s = RunStore(app.db, waiter_alive=lambda rec: True)
    return s


def test_waits_then_takes_slot(store, app):
    reserve(store, "busy")
    events = []

    def free_after_two(t):
        if t.sleeps == 2:
            store.finish("busy", status="ok", reason=None)

    t = FakeTime(free_after_two)
    run = reserve(store, "planned", wait=WaitPolicy(seconds=600, poll=5, on_wait=events.append, clock=t.clock, sleep=t.sleep))
    assert run.status == "running" and run.id == "planned"
    assert events[0] == {"state": "waiting", "running": 1, "ahead": 0, "limit": 1, "left_seconds": 600}
    assert events[-1]["state"] == "started" and events[-1]["waited_seconds"] == 10


def test_skipped_after_limit_stays_in_history(store):
    reserve(store, "busy")
    t = FakeTime()
    with pytest.raises(ReplayError) as e:
        reserve(store, "planned", wait=WaitPolicy(seconds=60, poll=20, clock=t.clock, sleep=t.sleep))
    assert e.value.code == "run_skipped" and "за 1 мин не освободился слот" in e.value.message
    rec = store.get("planned")
    assert (rec.status, rec.reason, rec.label) == ("failed", "skipped", "failed (skipped)")
    assert "busy" in rec.error and rec.finished_at


def test_manual_refused_at_once_and_does_not_jump_queue(store):
    reserve(store, "busy")
    with pytest.raises(ReplayError) as e:
        reserve(store, "manual")
    assert e.value.code == "parallel_limit"
    # слот освободился, но в очереди есть живой ждущий: ручной запуск место не забирает
    store._db.execute("INSERT INTO runs (id, script_name, script_path, status, created_at) VALUES ('w1', 's', 'x', 'waiting', 'now')")
    store.finish("busy", status="ok", reason=None)
    with pytest.raises(ReplayError) as e:
        reserve(store, "manual2")
    assert e.value.code == "parallel_limit" and "в очереди ждут ещё 1" in e.value.message
    assert e.value.details["waiting"] == ["w1"]


def test_dead_waiter_does_not_hold_queue(app):
    store = RunStore(app.db, waiter_alive=lambda rec: rec.id != "dead")
    store._db.execute("INSERT INTO runs (id, script_name, script_path, status, created_at) VALUES ('dead', 's', 'x', 'waiting', 'now')")
    assert reserve(store, "manual").status == "running"
    dead = store.get("dead")
    assert dead.label == "failed (lost)" and "пропал" in dead.error


def test_real_liveness_check(app):
    me = os.getpid()
    store = app.runs  # проверка по процессу: pid и время старта
    store._db.execute(
        "INSERT INTO runs (id, script_name, script_path, status, created_at, supervisor_pid, supervisor_started)"
        " VALUES ('alive', 's', 'x', 'waiting', 'now', ?, ?), ('gone', 's', 'x', 'waiting', 'now', ?, ?)",
        (me, procinfo.start_time(me), me, (procinfo.start_time(me) or 0) - 1000),
    )
    assert [r.id for r in store.waiting()] == ["alive", "gone"]
    launch.recover_lost(app)
    assert [r.id for r in store.waiting()] == ["alive"]
    assert store.get("gone").label == "failed (lost)"


def test_cancel_waiting(store, app):
    reserve(store, "busy")

    def cancel_on_first_sleep(t):
        launch.cancel(app, "planned")

    t = FakeTime(cancel_on_first_sleep)
    with pytest.raises(ReplayError) as e:
        reserve(store, "planned", wait=WaitPolicy(seconds=600, poll=1, clock=t.clock, sleep=t.sleep))
    assert e.value.code == "run_cancelled"
    assert store.get("planned").label == "failed (cancelled)"


def test_interrupt_leaves_queue(store):
    reserve(store, "busy")

    def interrupt(t):
        raise KeyboardInterrupt

    t = FakeTime(interrupt)
    with pytest.raises(KeyboardInterrupt):
        reserve(store, "planned", wait=WaitPolicy(seconds=600, poll=1, clock=t.clock, sleep=t.sleep))
    assert store.get("planned").label == "failed (cancelled)" and store.waiting() == []


# --- гонка настоящих процессов ---

WAITER = textwrap.dedent("""
    import json, sys, time
    from pathlib import Path
    from dawg_replay.app import App
    from dawg_replay.runs import WaitPolicy
    home, run_id, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    with App(Path(home)) as app:
        run = app.runs.reserve(run_id=run_id, script_name="s", script_version=1, script_path="x", draft=False,
                               preset=None, params={}, timeout_seconds=60, limit=1,
                               wait=WaitPolicy(seconds=60, poll=0.01))
        (out / f"{run_id}.started").write_text(str(time.time()))
        while not (out / f"{run_id}.done").exists():
            time.sleep(0.01)
        app.runs.finish(run_id, status="ok", reason=None)
""")


def test_race_of_waiters_is_fair_and_never_exceeds_limit(home, tmp_path):
    out = tmp_path / "race"
    out.mkdir()
    with App(home) as app:
        reserve(app.runs, "busy")
    script = tmp_path / "waiter.py"
    script.write_text(WAITER, encoding="utf-8")
    procs, order = [], ["w1", "w2", "w3"]
    try:
        for rid in order:  # встают в очередь строго по одному
            procs.append(subprocess.Popen([sys.executable, str(script), str(home), rid, str(out)]))
            deadline = time.monotonic() + 20
            while True:
                with App(home) as app:
                    if any(r.id == rid for r in app.runs.waiting()):
                        break
                assert time.monotonic() < deadline, f"{rid} не встал в очередь"
                time.sleep(0.02)
        with App(home) as app:
            assert [r.id for r in app.runs.waiting()] == order
            app.runs.finish("busy", status="ok", reason=None)

        started = []
        for expected in order:
            deadline = time.monotonic() + 20
            while not (out / f"{expected}.started").exists():
                with App(home) as app:
                    running = [r.id for r in app.runs.running()]
                assert len(running) <= 1, running
                assert time.monotonic() < deadline, f"{expected} не дождался слота"
                time.sleep(0.01)
            # слот занят ровно одним, остальные ещё ждут
            with App(home) as app:
                assert [r.id for r in app.runs.running()] == [expected]
                assert [r.id for r in app.runs.waiting()] == order[len(started) + 1:]
            started.append(expected)
            time.sleep(0.15)  # другие ждущие успевают сделать несколько попыток
            with App(home) as app:
                assert [r.id for r in app.runs.running()] == [expected]
            (out / f"{expected}.done").write_text("1")
        assert started == order
    finally:
        for p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
    assert [p.returncode for p in procs] == [0, 0, 0]


# --- CLI и интерфейс ---


@pytest.fixture
def ready(app, write_script):
    app.registry.add(write_script())
    app.save_preset("ozon-cards", "daily", {"query": "чайник"})
    return app


def busy_run(app):
    """Идущий запуск с живым «супервизором» (этот процесс): recover_lost его не закроет."""
    me = os.getpid()
    app.db.execute(
        "INSERT INTO runs (id, script_name, script_path, status, created_at, supervisor_pid, supervisor_started, heartbeat_at)"
        " VALUES ('busy', 'other', 'x', 'running', strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (me, procinfo.start_time(me)),
    )
    app.set_config("max_parallel_runs", "1")


def short_wait(monkeypatch, seconds=0.3):
    real = launch._wait_policy

    def policy(cfg, on_wait):
        p = real(cfg, on_wait)
        p.seconds, p.poll = seconds, 0.05
        return p

    monkeypatch.setattr(launch, "_wait_policy", policy)


def test_cli_scheduled_run_skipped(ready, cli, monkeypatch):
    s = ready.schedules.add("ozon-cards", "daily", __import__("dawg_replay.schedules", fromlist=["Spec"]).Spec.daily("09:00"))
    busy_run(ready)
    short_wait(monkeypatch)
    code, data = cli("run", "ozon-cards", "--preset", "daily", "--follow", "--schedule", s.id)
    assert code == 3 and data["ok"] and data["skipped"]
    run = data["run"]
    assert (run["label"], run["trigger"], run["schedule_id"]) == ("failed (skipped)", "schedule", s.id)
    with App(ready.paths.home) as a:
        assert a.runs.get(run["id"]).label == "failed (skipped)"


def test_cli_wait_text_goes_to_output(ready, capsys, monkeypatch):
    from dawg_replay.cli import main

    busy_run(ready)
    short_wait(monkeypatch)
    assert main(["run", "ozon-cards", "--preset", "daily", "--wait"]) == 3
    out = capsys.readouterr().out
    assert "лимит занят: идут 1 из 1" in out and "пропущен: за 0 мин" in out


def test_cli_manual_run_refused_at_once(ready, cli):
    busy_run(ready)
    code, data = cli("run", "ozon-cards", "--preset", "daily")
    assert code == 1 and data["error"]["code"] == "parallel_limit"


def test_dashboard_and_run_page_show_waiting(ready, monkeypatch):
    from starlette.testclient import TestClient

    from dawg_replay.web.security import Secrets
    from dawg_replay.web.server import create_app

    ready.db.execute(
        "INSERT INTO runs (id, script_name, script_path, status, created_at, supervisor_pid, supervisor_started, triggered_by, schedule_id)"
        " VALUES ('20261114-090000-aaaaaa', 'ozon-cards', 'x', 'waiting', '2026-11-14T09:00:00Z', ?, ?, 'schedule', 'abcd1234')",
        (os.getpid(), procinfo.start_time(os.getpid())),
    )
    ready.db.execute(
        "INSERT INTO runs (id, script_name, script_path, status, reason, created_at, finished_at)"
        " VALUES ('20261114-080000-bbbbbb', 'ozon-cards', 'x', 'failed', 'skipped', '2026-11-14T08:00:00Z', '2026-11-14T08:30:00Z')"
    )
    secrets_ = Secrets(port=54324)
    base = "http://127.0.0.1:54324"
    with TestClient(create_app(ready.paths.home, secrets_), base_url=base, follow_redirects=False) as c:
        c.get(f"/?token={secrets_.token}")
        h = c.get("/").text
        assert "Ждут свободного слота" in h and "Убрать из очереди" in h and "по расписанию, в очереди" in h
        assert "пропущен" in h  # «Требует внимания»
        page = c.get("/runs/20261114-090000-aaaaaa").text
        assert "Ждёт свободного слота" in page and "ждёт слот" in page
        skipped = c.get("/runs/20261114-080000-bbbbbb").text
        assert "время ожидания вышло" in skipped
        r = c.post("/runs/20261114-090000-aaaaaa/cancel", headers={"Origin": base, "X-CSRF-Token": secrets_.csrf})
        assert r.status_code == 200
    assert ready.runs.get("20261114-090000-aaaaaa").label == "failed (cancelled)"
