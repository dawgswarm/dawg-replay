import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dawg_replay_kit.runtime import Kit, StartupError

SCRIPT_BODY = '''
import dawg_replay_kit as kit

kit.stage("Старт")
kit.log.info(f"запрос {kit.params.query}, токен {kit.params.sheet_token}")
for i in range(1, kit.params.max_pages + 1):
    kit.progress("pages", "Страницы", i, kit.params.max_pages)
    kit.metric("seen", "Просмотрено", i * 10)
(kit.output_dir / "out.csv").write_text("a\\n1\\n", encoding="utf-8")
kit.artifact("out.csv", "CSV")
kit.rows("cards", [{"title": "A"}])
print("обычный print в stdout")
kit.result("ok", "готово")
'''


def run_script(path, args=(), env=None, cwd=None):
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(("DAWG_SCRIPT_", "DAWG_SECRET_"))}
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, path, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full_env,
        cwd=cwd,
    )


def test_app_mode_end_to_end(script_file, tmp_path):
    path = script_file(SCRIPT_BODY)
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"query": "ноутбук", "max_pages": 3, "sheet_token": "s3cret-value"}), encoding="utf-8")
    events = tmp_path / "events.jsonl"
    out = tmp_path / "run-out"
    r = run_script(
        path,
        env={
            "DAWG_SCRIPT_PARAMS": str(params),
            "DAWG_SCRIPT_EVENTS": str(events),
            "DAWG_SCRIPT_OUTPUT_DIR": str(out),
            "DAWG_SCRIPT_RUN_ID": "run-42",
        },
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "обычный print в stdout"
    assert r.stderr == ""
    evs = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert all("ts" in e for e in evs)
    types = [e["t"] for e in evs]
    assert types[0] == "stage" and types[-1] == "result"
    assert evs[1]["msg"] == "запрос ноутбук, токен ***"
    progress = [e for e in evs if e["t"] == "progress"]
    assert progress[-1]["done"] == 3  # последнее значение дошло
    metrics = [e for e in evs if e["t"] == "metric"]
    assert metrics[-1]["value"] == 30
    art = next(e for e in evs if e["t"] == "artifact")
    assert (art["path"], art["label"], art["mime"]) == ("out.csv", "CSV", "text/csv")
    assert (out / "out.csv").exists()
    assert "s3cret-value" not in events.read_text(encoding="utf-8")


def test_autonomous_end_to_end(script_file, tmp_path):
    path = script_file(SCRIPT_BODY)
    r = run_script(path, ["--query", "q", "--max-pages", "2"], env={"DAWG_SECRET_SHEET_TOKEN": "abcd1234"}, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "==> Старт" in r.stderr
    assert "токен ***" in r.stderr and "abcd1234" not in r.stderr
    assert "Страницы: 2/2" in r.stderr
    assert "ИТОГ ok: готово" in r.stderr
    assert (tmp_path / "output" / "out.csv").exists()


def test_autonomous_missing_required(script_file, tmp_path):
    path = script_file(SCRIPT_BODY)
    r = run_script(path, [], cwd=tmp_path)
    assert r.returncode == 2
    assert "dawg-replay-kit:" in r.stderr and "--query" in r.stderr
    assert "Traceback" not in r.stderr


def test_autonomous_help(script_file, tmp_path):
    path = script_file(SCRIPT_BODY)
    r = run_script(path, ["--help"], cwd=tmp_path)
    assert r.returncode == 0
    assert "--max-pages" in r.stdout and "DAWG_SECRET_SHEET_TOKEN" in r.stdout


def test_app_mode_bad_params_goes_to_events(script_file, tmp_path):
    path = script_file(SCRIPT_BODY)
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"max_pages": 0}), encoding="utf-8")
    events = tmp_path / "events.jsonl"
    r = run_script(
        path,
        env={"DAWG_SCRIPT_PARAMS": str(params), "DAWG_SCRIPT_EVENTS": str(events), "DAWG_SCRIPT_OUTPUT_DIR": str(tmp_path / "o")},
    )
    assert r.returncode == 2
    (ev,) = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert ev["t"] == "log" and ev["level"] == "error"
    assert "`query`" in ev["msg"] and "`max_pages`" in ev["msg"]
    assert "`query`" in r.stderr


def test_bad_manifest_stops(tmp_path):
    f = tmp_path / "script.py"
    f.write_text("import dawg_replay_kit as kit\nkit.stage('x')\n", encoding="utf-8")
    r = run_script(str(f), cwd=tmp_path)
    assert r.returncode == 2
    assert "манифест скрипта" in r.stderr and "dawg-script" in r.stderr


def test_kit_from_environment_direct(script_file, tmp_path):
    path = script_file()
    kit = Kit.from_environment(script_path=path, argv=["--query", "q"], environ={}, cwd=tmp_path)
    assert kit.autonomous is True
    assert kit.run_id is None
    assert kit.output_dir == tmp_path / "output" and kit.output_dir.is_dir()
    assert kit.params.query == "q"
    kit.close()


def test_kit_from_environment_errors(script_file, tmp_path):
    with pytest.raises(StartupError):
        Kit.from_environment(script_path=script_file(), argv=[], environ={}, cwd=tmp_path)
    with pytest.raises(StartupError):
        Kit.from_environment(script_path=tmp_path / "nope.py", argv=[], environ={}, cwd=tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM")
def test_autonomous_sigterm_neutral_text(script_file, tmp_path):
    import signal
    import time

    path = script_file('''
import time
import dawg_replay_kit as kit

kit.log.info("старт")
(kit.output_dir / "started").write_text("1")
time.sleep(60)
''')
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DAWG_SCRIPT_", "DAWG_SECRET_"))}
    p = subprocess.Popen(
        [sys.executable, path, "--query", "q"], cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    end = time.monotonic() + 15
    while not (tmp_path / "output" / "started").exists() and time.monotonic() < end:
        time.sleep(0.02)
    p.send_signal(signal.SIGTERM)
    _, err = p.communicate(timeout=15)
    assert p.returncode == 143
    assert "запуск остановлен сигналом" in err
    assert "приложением" not in err and "отмены" not in err
