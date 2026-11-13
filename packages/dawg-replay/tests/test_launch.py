"""Интеграция запуска: run, runs, logs, cancel на настоящих процессах."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dawg_replay import hooks, launch
from dawg_replay.app import App
from dawg_replay.cli_run import EXIT_RUN_FAILED

POSIX = sys.platform != "win32"

SCRIPT = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit"]
# ///

# /// dawg-script
# contract = 1
# name = "{name}"
# title = "Тест запуска"
# version = {version}
# timeout_minutes = 90
#
# [[params]]
# name = "query"
# type = "string"
# title = "Запрос"
# required = true
#
# [[params]]
# name = "pages"
# type = "int"
# title = "Страницы"
# default = 1
#
# [[params]]
# name = "sort"
# type = "string"
# title = "Сортировка"
# default = "new"
#
# [[params]]
# name = "token"
# type = "secret"
# title = "Токен"
# ///
import json, os, time
import dawg_replay_kit as kit

out = kit.output_dir
(out / "seen.json").write_text(json.dumps({{
    "values": {{"query": kit.params.query, "pages": kit.params.pages, "sort": kit.params.sort}},
    "token": kit.params.token,
    "pid": os.getpid(),
}}), encoding="utf-8")
print("stdout строка")
try:
    if kit.params.query == "долго":
        kit.stage("ждём")
        time.sleep(120)
    kit.result("ok", f"готово: {{kit.params.query}}")
finally:
    kit.log.info("finally скрипта")
'''


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def make_script(tmp_path):
    def make(name="launch-test", version=1):
        f = tmp_path / "src" / f"{name}-{version}.py"
        f.parent.mkdir(exist_ok=True)
        f.write_text(SCRIPT.format(name=name, version=version), encoding="utf-8")
        return f

    return make


@pytest.fixture
def registered(cli, make_script, kr):
    code, _ = cli("add", str(make_script()))
    assert code == 0
    kr.set_password("dawg-replay", "secret:launch-test:token", "tok-SECRET-1")
    kr.set_password("dawg-replay", "dawg-api-key", "api-KEY-1")
    return "launch-test"


def wait_for(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def seen(home, run_id):
    f = Path(home) / "runs" / run_id / "output" / "seen.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def run_record(home, run_id):
    with App(Path(home)) as a:
        return a.runs.get(run_id)


def alive(pid):
    return launch.pid_alive(pid)


# --- значения и итог ----------------------------------------------------------


def test_follow_run_merges_values_and_records_outcome(cli, registered, home, tmp_path):
    cli("preset", "save", registered, "base", "--set", "query=из-пресета", "--set", "pages=2", "--set", "sort=old")
    pfile = tmp_path / "p.json"
    pfile.write_text(json.dumps({"pages": 3, "query": "из-файла"}), encoding="utf-8")

    code, data = cli("run", registered, "--preset", "base", "--params", str(pfile), "--set", "query=из-set", "--follow")
    assert code == 0, data
    run = data["run"]
    assert (run["status"], run["label"], run["exit_code"]) == ("ok", "ok", 0)
    assert run["summary"] == "готово: из-set"
    assert (run["version"], run["draft"], run["preset"]) == (1, False, "base")
    # --set > --params > пресет > default
    s = seen(home, run["id"])
    assert s["values"] == {"query": "из-set", "pages": 3, "sort": "old"}
    assert s["token"] == "tok-SECRET-1"
    # Секрет не попал ни в БД, ни в каталог запуска.
    assert "token" not in run["params"]
    run_dir = Path(data["run_dir"])
    assert json.loads((run_dir / "params.json").read_text(encoding="utf-8")) == {
        "query": "из-set", "pages": 3, "sort": "old"
    }
    for f in run_dir.rglob("*"):
        if f.is_file() and f.name != "seen.json":
            assert "tok-SECRET-1" not in f.read_text(encoding="utf-8", errors="replace"), f
    assert list((Path(home) / "tmp").iterdir()) == []
    # Таймаут: min(timeout_minutes, max_timeout_minutes).
    assert run["timeout_seconds"] == 90 * 60

    code, data = cli("runs", registered)
    assert [r["id"] for r in data["runs"]] == [run["id"]]


def test_timeout_capped_by_config(cli, registered):
    cli("config", "set", "max_timeout_minutes", "2")
    code, data = cli("run", registered, "--set", "query=x", "--follow")
    assert data["run"]["timeout_seconds"] == 120


def test_failed_follow_run_exit_code(cli, registered, home, make_script, tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(make_script().read_text(encoding="utf-8").replace("kit.result(", "raise SystemExit(5)\n    kit.result("), encoding="utf-8")
    code, data = cli("run", str(bad), "--set", "query=x", "--follow")
    assert code == EXIT_RUN_FAILED
    assert (data["run"]["label"], data["run"]["exit_code"]) == ("failed", 5)


def test_draft_run_copies_file(cli, make_script, kr, home):
    src = make_script(name="draft-one")
    kr.set_password("dawg-replay", "secret:draft-one:token", "t")
    code, data = cli("run", str(src), "--set", "query=черновик", "--follow")
    assert code == 0, data
    run = data["run"]
    assert run["draft"] is True and run["version"] is None and run["script"] == "draft-one"
    assert Path(run["script_path"]) == Path(data["run_dir"]) / "script.py"
    assert Path(run["script_path"]).read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_secret_cannot_be_passed_on_command_line(cli, registered):
    code, data = cli("run", registered, "--set", "query=x", "--set", "token=leak", "--follow")
    assert code == 1 and data["error"]["code"] == "bad_params"
    assert "secret set" in data["error"]["message"]


def test_missing_required_values_are_explained(cli, make_script):
    code, data = cli("run", str(make_script(name="no-values")), "--follow")
    assert code == 1 and data["error"]["code"] == "bad_params"
    assert "query" in data["error"]["message"]


def test_unknown_script(cli, home):
    code, data = cli("run", "nope")
    assert data["error"]["code"] == "script_not_found"


def test_hooks_get_outcome(cli, registered):
    got = []

    def hook(outcome):
        got.append((outcome.status, outcome.run_dir.name))

    def broken(outcome):
        raise RuntimeError("сломался")

    hooks.register(hook)
    hooks.register(broken)
    try:
        code, data = cli("run", registered, "--set", "query=x", "--follow")
    finally:
        hooks.unregister(hook)
        hooks.unregister(broken)
    assert got == [("ok", data["run"]["id"])]
    assert "сломался" in data["run"]["error"]


# --- фоновый запуск, лимит, отмена --------------------------------------------


def start_background(cli, registered, query="долго"):
    code, data = cli("run", registered, "--set", f"query={query}")
    assert code == 0, data
    return data["run"]["id"], data["supervisor_pid"]


def test_background_run_finishes_on_its_own(cli, registered, home):
    run_id, sup = start_background(cli, registered, query="быстро")
    assert wait_for(lambda: run_record(home, run_id).status != "running")
    rec = run_record(home, run_id)
    assert (rec.label, rec.summary) == ("ok", "готово: быстро")
    assert wait_for(lambda: not alive(sup), 5)

    code, data = cli("logs", run_id)
    assert "stdout строка" in data["stdout"]
    code, data = cli("logs", run_id, "--events")
    assert [e["t"] for e in data["events"]][-2:] == ["result", "log"]


def test_parallel_limit_and_cancel_from_other_process(cli, registered, home):
    cli("config", "set", "max_parallel_runs", "1")
    cli("config", "set", "cancel_grace_seconds", "5")
    run_id, sup = start_background(cli, registered)
    assert wait_for(lambda: seen(home, run_id) is not None)
    script_pid = seen(home, run_id)["pid"]

    code, data = cli("run", registered, "--set", "query=второй")
    assert code == 1 and data["error"]["code"] == "parallel_limit"
    assert run_id in data["error"]["message"]

    # Отмена из отдельного процесса CLI, как её вызовет человек или агент.
    env = {**os.environ, "DAWG_REPLAY_HOME": str(home)}
    p = subprocess.run(
        [sys.executable, "-m", "dawg_replay", "--json", "cancel", run_id],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )
    assert p.returncode == 0, p.stderr
    run = json.loads(p.stdout)["run"]
    assert (run["label"], run["killed"]) == ("failed (cancelled)", False)
    assert wait_for(lambda: not alive(script_pid) and not alive(sup), 5)
    code, data = cli("logs", run_id, "--events")
    assert "finally скрипта" in [e.get("msg") for e in data["events"]]

    code, data = cli("cancel", run_id)
    assert data["error"]["code"] == "not_running"
    # Лимит освободился.
    code, data = cli("run", registered, "--set", "query=после", "--follow")
    assert code == 0


def test_follow_interrupt_cancels(registered, home, kr):
    with App(Path(home), keyring_backend=kr) as app:
        prepared = launch.prepare(app, registered, sets=["query=долго"])
        interrupted = threading.Event()
        run_id = prepared.run.id
        threading.Thread(
            target=lambda: (wait_for(lambda: seen(home, run_id) is not None), interrupted.set()), daemon=True
        ).start()
        rec = launch.supervise(app, run_id, prepared.payload(), interrupted=interrupted)
    assert rec.label == "failed (cancelled)"


# --- потерянные запуски -------------------------------------------------------


@pytest.mark.skipif(not POSIX, reason="kill -9 супервизора проверяется на POSIX")
def test_lost_run_recovered(cli, registered, home, monkeypatch):
    run_id, sup = start_background(cli, registered)
    assert wait_for(lambda: seen(home, run_id) is not None)
    script_pid = seen(home, run_id)["pid"]
    secret_dirs = list((Path(home) / "tmp").iterdir())
    assert [d.name.startswith(run_id) for d in secret_dirs] == [True]
    stray = Path(home) / "tmp" / "20200101-000000-dead00-abc"
    stray.mkdir()

    os.kill(sup, signal.SIGKILL)
    assert wait_for(lambda: not alive(sup), 5)
    assert alive(script_pid)  # скрипт пережил супервизор

    got = []
    hooks.register(got.append)
    monkeypatch.setattr(launch, "STALE_SECONDS", 0)
    try:
        code, data = cli("runs")
    finally:
        hooks.unregister(got.append)
    run = next(r for r in data["runs"] if r["id"] == run_id)
    assert run["label"] == "failed (lost)"
    assert [o.reason for o in got] == ["lost"]
    assert wait_for(lambda: not alive(script_pid), 5)
    assert list((Path(home) / "tmp").iterdir()) == []


@pytest.mark.skipif(not POSIX, reason="kill -9 супервизора проверяется на POSIX")
def test_cancel_without_supervisor_stops_script_softly(cli, registered, home):
    cli("config", "set", "cancel_grace_seconds", "5")
    run_id, sup = start_background(cli, registered)
    assert wait_for(lambda: seen(home, run_id) is not None)
    script_pid = seen(home, run_id)["pid"]
    os.kill(sup, signal.SIGKILL)
    assert wait_for(lambda: not alive(sup), 5)

    code, data = cli("cancel", run_id)
    assert code == 0, data
    assert (data["run"]["label"], data["run"]["killed"]) == ("failed (cancelled)", False)
    assert wait_for(lambda: not alive(script_pid), 5)
    code, data = cli("logs", run_id, "--events")
    assert "finally скрипта" in [e.get("msg") for e in data["events"]]
    assert list((Path(home) / "tmp").iterdir()) == []


def test_fresh_running_record_is_not_lost(registered, home, kr):
    with App(Path(home), keyring_backend=kr) as app:
        prepared = launch.prepare(app, registered, sets=["query=x"])
        assert launch.recover_lost(app) == []
        assert app.runs.get(prepared.run.id).running
        # Запись без супервизора, которая давно не подаёт признаков жизни, закрывается.
        app.db.execute("UPDATE runs SET created_at = '2020-01-01T00:00:00Z' WHERE id = ?", (prepared.run.id,))
        lost = launch.recover_lost(app)
        assert [r.label for r in lost] == ["failed (lost)"]


def test_script_command_override(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, "python")
    with pytest.raises(Exception, match="JSON-списком"):
        launch.script_command()
    monkeypatch.delenv(launch.ENV_SCRIPT_COMMAND)
    assert launch.script_command() == ["uv", "run"]
