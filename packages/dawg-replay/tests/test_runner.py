"""Раннер на настоящих дочерних процессах: итог, отмена, kill дерева, таймаут."""

import json
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dawg_replay.runner import (
    REASON_CANCELLED,
    REASON_EXIT_CODE,
    REASON_TIMEOUT,
    RunnerError,
    ScriptRun,
    decide,
    last_result,
)

POSIX = sys.platform != "win32"

MANIFEST = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit"]
# ///

# /// dawg-script
# contract = 1
# name = "runner-test"
# title = "Тест раннера"
# version = 1
#
# [[params]]
# name = "query"
# type = "string"
# title = "Запрос"
#
# [[params]]
# name = "token"
# type = "secret"
# title = "Токен"
# ///
'''


@pytest.fixture
def script(tmp_path):
    def write(body: str, manifest: bool = True) -> Path:
        f = tmp_path / "script.py"
        f.write_text((MANIFEST if manifest else "") + "\n" + textwrap.dedent(body), encoding="utf-8")
        return f

    return write


@pytest.fixture
def runs_dir(tmp_path):
    return tmp_path / "runs"


def make_run(script_path, runs_dir, **kw):
    kw.setdefault("command", [sys.executable])
    kw.setdefault("run_id", "r1")
    return ScriptRun(script_path, runs_dir=runs_dir, **kw)


def events(run):
    return [json.loads(line) for line in run.events_path.read_text(encoding="utf-8").splitlines()]


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- итог по 5.4 -------------------------------------------------------------


def test_successful_run_layout_env_and_secrets(script, runs_dir):
    path = script('''
        import json, os
        import dawg_replay_kit as kit
        env = {k: os.environ.get(k) for k in (
            "DAWG_API_KEY", "DAWG_BASE_URL", "DAWG_SCRIPT_RUN_ID", "DAWG_SCRIPT_OUTPUT_DIR")}
        env["params_file"] = os.environ["DAWG_SCRIPT_PARAMS"]
        env["mode"] = oct(os.stat(env["params_file"]).st_mode & 0o777)
        (kit.output_dir / "env.json").write_text(json.dumps(env), encoding="utf-8")
        print("в stdout")
        import sys; print("в stderr", file=sys.stderr)
        kit.log.info(f"токен {kit.params.token}")
        kit.result("partial", f"запрос {kit.params.query}")
    ''')
    run = make_run(
        path, runs_dir,
        values={"query": "ноутбук", "token": "s3cr3t-value"}, secret_names={"token"},
        api_key="key-123", base_url="https://example.test",
    )
    outcome = run.run()

    assert outcome.exit_code == 0, run.stderr_path.read_text()
    assert (outcome.status, outcome.reason, outcome.label) == ("partial", None, "partial")
    assert outcome.summary == "запрос ноутбук"
    assert run.run_dir == runs_dir / "r1"
    assert json.loads(run.params_path.read_text(encoding="utf-8")) == {"query": "ноутбук"}
    assert "в stdout" in run.stdout_path.read_text(encoding="utf-8")
    assert "в stderr" in run.stderr_path.read_text(encoding="utf-8")

    env = json.loads((run.output_dir / "env.json").read_text(encoding="utf-8"))
    assert env["DAWG_API_KEY"] == "key-123"
    assert env["DAWG_BASE_URL"] == "https://example.test"
    assert env["DAWG_SCRIPT_RUN_ID"] == "r1"
    assert Path(env["DAWG_SCRIPT_OUTPUT_DIR"]) == run.output_dir.resolve()
    # Файл с секретами вне каталога запуска, 0600, после выхода удалён.
    secret_file = Path(env["params_file"])
    assert run.run_dir.resolve() not in secret_file.resolve().parents
    if POSIX:
        assert env["mode"] == "0o600"
    assert not secret_file.exists()
    assert not secret_file.parent.exists()
    # Секрет нигде в каталоге запуска.
    for f in run.run_dir.rglob("*"):
        if f.is_file():
            assert "s3cr3t-value" not in f.read_text(encoding="utf-8", errors="replace"), f


def test_without_secrets_script_reads_run_params(script, runs_dir):
    path = script('''
        import os, dawg_replay_kit as kit
        kit.result("ok", os.environ["DAWG_SCRIPT_PARAMS"])
    ''')
    run = make_run(path, runs_dir, values={"query": "x"}, secret_names={"token"})
    outcome = run.run()
    assert outcome.status == "ok"
    assert Path(outcome.summary) == run.params_path


def test_inherited_dawg_variables_do_not_leak(script, runs_dir, monkeypatch):
    monkeypatch.setenv("DAWG_API_KEY", "чужой")
    monkeypatch.setenv("DAWG_SECRET_TOKEN", "чужой")
    monkeypatch.setenv("DAWG_SCRIPT_RUN_ID", "чужой")
    path = script('''
        import os, sys
        sys.exit(0 if all(os.environ.get(k) != "чужой" for k in
                 ("DAWG_API_KEY", "DAWG_SECRET_TOKEN", "DAWG_SCRIPT_RUN_ID")) else 7)
    ''', manifest=False)
    assert make_run(path, runs_dir).run().exit_code == 0


def test_exit_zero_without_result_is_ok(script, runs_dir):
    outcome = make_run(script("print('hi')\n", manifest=False), runs_dir).run()
    assert (outcome.status, outcome.reason, outcome.result) == ("ok", None, None)


def test_nonzero_exit_is_failed_even_after_result_ok(script, runs_dir):
    path = script('''
        import dawg_replay_kit as kit
        kit.result("ok", "всё хорошо")
        raise RuntimeError("упал после result")
    ''')
    outcome = make_run(path, runs_dir).run()
    assert outcome.exit_code == 1
    assert (outcome.status, outcome.reason) == ("failed", REASON_EXIT_CODE)
    assert outcome.summary == "всё хорошо"
    assert "упал после result" in (runs_dir / "r1" / "stderr.log").read_text(encoding="utf-8")


def test_last_result_wins_and_broken_lines_are_skipped(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"t":"result","status":"ok","summary":"первый"}\n'
        "не json\n"
        '{"t":"result","status":"странный","summary":"x"}\n'
        '{"t":"result","status":"partial","summary":"последний"}\n'
        '{"t":"log","level":"info","msg":"после"}\n'
        '{"t":"res',
        encoding="utf-8",
    )
    assert last_result(f)["summary"] == "последний"
    assert last_result(tmp_path / "нет.jsonl") is None


@pytest.mark.parametrize(
    "code,result,stop,expected",
    [
        (0, None, None, ("ok", None)),
        (0, {"status": "partial"}, None, ("partial", None)),
        (0, {"status": "failed"}, None, ("failed", None)),
        (3, {"status": "ok"}, None, ("failed", REASON_EXIT_CODE)),
        (0, {"status": "ok"}, REASON_CANCELLED, ("failed", REASON_CANCELLED)),
        (-9, None, REASON_TIMEOUT, ("failed", REASON_TIMEOUT)),
    ],
)
def test_decide(code, result, stop, expected):
    assert decide(code, result, stop) == expected


def test_start_errors(script, runs_dir, tmp_path):
    with pytest.raises(RunnerError, match="файла скрипта нет"):
        make_run(tmp_path / "нет.py", runs_dir).start()
    path = script("pass\n", manifest=False)
    run = make_run(path, runs_dir, command=[str(tmp_path / "нет-такой-программы")],
                   values={"token": "t"}, secret_names={"token"})
    with pytest.raises(RunnerError, match="не удалось запустить"):
        run.start()
    assert run._secret_dir is None


# --- хуки --------------------------------------------------------------------


def test_after_exit_hooks_get_outcome_and_failures_are_recorded(script, runs_dir):
    seen = []

    def insurance(outcome):
        seen.append((outcome.status, outcome.exit_code, outcome.run_dir.exists()))

    def broken(outcome):
        raise ValueError("сломался")

    outcome = make_run(script("pass\n", manifest=False), runs_dir, after_exit=[broken, insurance]).run()
    assert seen == [("ok", 0, True)]
    assert len(outcome.hook_errors) == 1 and "сломался" in outcome.hook_errors[0]


# --- отмена и таймаут по 5.7 ------------------------------------------------

KIT_CANCEL_BODY = '''
    import atexit, time
    import dawg_replay_kit as kit

    atexit.register(lambda: (kit.output_dir / "atexit").write_text("1"))
    try:
        kit.progress("p", "Шаги", 1, 100)
        kit.progress("p", "Шаги", 42, 100)   # отложен прореживанием
        (kit.output_dir / "started").write_text("1")
        time.sleep(60)
    except Exception:
        kit.log.error("SystemExit не должен ловиться как Exception")
    finally:
        kit.log.info("finally скрипта")
'''


def test_soft_cancel_runs_finally_atexit_and_flushes_pending_events(script, runs_dir):
    run = make_run(script(KIT_CANCEL_BODY), runs_dir, grace=10)
    run.start()
    assert wait_for(lambda: (run.output_dir / "started").exists())
    t0 = time.monotonic()
    run.cancel()
    outcome = run.wait(15)
    assert outcome is not None
    assert time.monotonic() - t0 < 5  # ушёл сам, kill не понадобился
    assert (outcome.status, outcome.reason, outcome.label) == ("failed", REASON_CANCELLED, "failed (cancelled)")
    assert outcome.killed is False
    assert outcome.exit_code == 143

    evs = events(run)
    msgs = [e.get("msg") for e in evs if e["t"] == "log"]
    assert "finally скрипта" in msgs
    assert (run.output_dir / "atexit").exists()
    assert "SystemExit не должен ловиться как Exception" not in msgs
    assert msgs[-1] == "запуск остановлен приложением (отмена или таймаут)"
    assert [e["done"] for e in evs if e["t"] == "progress"] == [1, 42]


@pytest.mark.skipif(not POSIX, reason="порядок сигналов проверяется на POSIX")
def test_repeated_signal_does_not_interrupt_cleanup(script, runs_dir):
    path = script('''
        import os, signal, time
        import dawg_replay_kit as kit
        kit.init()
        try:
            (kit.output_dir / "started").write_text("1")
            time.sleep(60)
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # как если бы uv переслал сигнал ещё раз
            time.sleep(0.2)
            kit.log.info("уборка дошла до конца")
    ''')
    run = make_run(path, runs_dir, grace=10)
    run.start()
    assert wait_for(lambda: (run.output_dir / "started").exists())
    run.cancel()
    outcome = run.wait(15)
    assert outcome.exit_code == 143
    assert "уборка дошла до конца" in [e.get("msg") for e in events(run)]


HARD_BODY = '''
    import os, signal, subprocess, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c",
        "import signal, time\\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "time.sleep(120)\\n"])
    out = os.environ["DAWG_SCRIPT_OUTPUT_DIR"]
    with open(os.path.join(out, "pids.tmp"), "w") as f:
        f.write(f"{os.getpid()} {child.pid}")
    os.replace(os.path.join(out, "pids.tmp"), os.path.join(out, "pids"))
    time.sleep(120)
'''


def read_pids(run):
    assert wait_for(lambda: (run.output_dir / "pids").exists())
    return [int(p) for p in (run.output_dir / "pids").read_text().split()]


def test_hard_kill_of_whole_tree_after_grace(script, runs_dir):
    run = make_run(script(HARD_BODY, manifest=False), runs_dir, grace=1)
    run.start()
    parent, child = read_pids(run)
    assert alive(child)
    t0 = time.monotonic()
    run.cancel()
    outcome = run.wait(15)
    elapsed = time.monotonic() - t0
    assert outcome is not None
    assert 1 <= elapsed < 8
    assert (outcome.label, outcome.killed) == ("failed (cancelled)", True)
    assert wait_for(lambda: not alive(parent) and not alive(child), 5)


def test_timeout_uses_same_procedure(script, runs_dir):
    run = make_run(script(HARD_BODY, manifest=False), runs_dir, grace=0.5, timeout=1)
    t0 = time.monotonic()
    run.start()
    _, child = read_pids(run)
    outcome = run.wait(15)
    assert outcome is not None
    assert time.monotonic() - t0 < 8
    assert (outcome.status, outcome.reason, outcome.label) == ("failed", REASON_TIMEOUT, "failed (timeout)")
    assert outcome.killed is True
    assert wait_for(lambda: not alive(child), 5)


def test_timeout_with_soft_stop_keeps_timeout_reason(script, runs_dir):
    run = make_run(script(KIT_CANCEL_BODY), runs_dir, timeout=1, grace=10)
    outcome = run.run()
    assert (outcome.label, outcome.killed) == ("failed (timeout)", False)
    msgs = [e.get("msg") for e in events(run) if e["t"] == "log"]
    assert "finally скрипта" in msgs
    assert msgs[-1] == "запуск остановлен приложением (отмена или таймаут)"
    assert not any("отмены" in m for m in msgs)


@pytest.mark.skipif(not POSIX, reason="на Windows taskkill нужен живой корень дерева")
def test_processes_left_after_normal_exit_are_killed(script, runs_dir):
    path = script('''
        import os, subprocess, sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open(os.path.join(os.environ["DAWG_SCRIPT_OUTPUT_DIR"], "child"), "w").write(str(child.pid))
    ''', manifest=False)
    run = make_run(path, runs_dir)
    outcome = run.run()
    assert outcome.status == "ok"
    child = int((run.output_dir / "child").read_text())
    assert wait_for(lambda: not alive(child), 5)


# --- настоящий uv run --------------------------------------------------------


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv не установлен")
def test_cancel_through_uv_run_reaches_script(script, runs_dir):
    # Без зависимостей: uv не ходит в сеть, проверяем доставку сигнала через uv.
    path = script('''
        # /// script
        # requires-python = ">=3.10"
        # dependencies = []
        # ///
        import os, signal, sys, time
        out = os.environ["DAWG_SCRIPT_OUTPUT_DIR"]
        hits = []
        def on_term(signum, frame):
            hits.append(signum)
            if len(hits) == 1:
                raise SystemExit(143)
        signal.signal(signal.SIGTERM, on_term)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, on_term)
        open(os.path.join(out, "started"), "w").write(str(os.getpid()))
        try:
            time.sleep(60)
        finally:
            time.sleep(0.3)
            open(os.path.join(out, "finally"), "w").write(str(len(hits)))
    ''', manifest=False)
    run = make_run(path, runs_dir, command=["uv", "run", "--quiet"], grace=10)
    run.start()
    assert wait_for(lambda: (run.output_dir / "started").exists(), 120), run.stderr_path.read_text()
    run.cancel()
    outcome = run.wait(30)
    assert outcome is not None
    assert outcome.killed is False
    assert outcome.label == "failed (cancelled)"
    assert (run.output_dir / "finally").exists()
