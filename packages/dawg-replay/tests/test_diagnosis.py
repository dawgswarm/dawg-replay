"""Упавший запуск без событий: хвост stderr и подсказки (dawg_replay.diagnosis)."""

import json

import pytest
from starlette.testclient import TestClient

from dawg_replay import diagnosis
from dawg_replay.app import App
from dawg_replay.cli import main
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54323
BASE = f"http://127.0.0.1:{PORT}"
UV_KIT = (
    "  × No solution found when resolving script dependencies:\n"
    "  ╰─▶ Because dawg-replay-kit was not found in the package registry and you require dawg-replay-kit>=1.1,<2,\n"
    "      we can conclude that your requirements are unsatisfiable.\n"
)


@pytest.fixture
def client(home, kr):
    s = Secrets(port=PORT)
    with TestClient(create_app(home, s, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={s.token}").status_code == 303
        yield c


@pytest.fixture
def failed_run(home, write_script):
    with App(home) as app:
        app.registry.add(write_script(version=1))

    def make(stderr="", events=(), run_id="20261114-130000-aaaaaa", reason="exit_code", status="failed", summary=None):
        with App(home) as app:
            app.runs.reserve(run_id=run_id, script_name="ozon-cards", script_version=1, script_path="/x/script.py",
                             draft=False, preset=None, params={}, timeout_seconds=60, limit=100)
            run_dir = app.paths.run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "stderr.log").write_text(stderr, encoding="utf-8")
            (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
            app.runs.finish(run_id, status=status, reason=reason, exit_code=1, summary=summary)
        return run_id

    return make


def test_read_tail(tmp_path):
    f = tmp_path / "e.log"
    f.write_text("".join(f"line {i}\n" for i in range(50)) + "\n\n", encoding="utf-8")
    assert diagnosis.read_tail(f, 3) == ["line 47", "line 48", "line 49"]
    f.write_text("x" * 100 + "\nlast\n", encoding="utf-8")
    assert diagnosis.read_tail(f, 20, max_bytes=10) == ["last"]  # обрезанная первая строка отброшена
    assert diagnosis.read_tail(tmp_path / "nope.log") == []


def test_hint_survives_line_wrap():
    wrapped = "  ╰─▶ Because dawg-replay-kit was not found in the\n      package registry and you require it\n"
    assert diagnosis.hint_for(wrapped) == diagnosis.KIT_HINT
    assert diagnosis.hint_for("all good") is None


def test_mask():
    assert diagnosis.mask("key=abcd1234 tok=zz", ["abcd1234", "zz", ""]) == "key=*** tok=zz"


def test_kit_not_found_on_run_screen(client, failed_run, kr):
    kr.set_password("dawg-replay", "dawg-api-key", "SECRETKEY99")
    run_id = failed_run(stderr="warning: something\nusing key SECRETKEY99\n" + UV_KIT)
    h = client.get(f"/runs/{run_id}").text
    assert "Скрипт завершился с ошибкой (код 1)." in h and "видно в журнале ниже" not in h
    assert "журнал пуст" in h and "stderr-tail" in h
    assert "dev_kit_source" in h and "kit не найден" in h
    assert "was not found in the package registry" in h
    assert "SECRETKEY99" not in h and "using key ***" in h
    assert f"/runs/{run_id}/stderr.log" in h and f"dawg-replay logs {run_id}" in h
    r = client.get(f"/runs/{run_id}/stderr.log")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert "SECRETKEY99" not in r.text and "was not found" in r.text


def test_tail_is_escaped_and_limited(client, failed_run):
    lines = [f"строка {i}" for i in range(40)] + ["<script>alert(1)</script>"]
    run_id = failed_run(stderr="\n".join(lines) + "\n")
    h = client.get(f"/runs/{run_id}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h and "<script>alert(1)" not in h
    # Хвост на экране: 20 строк. В тексте для агента (панель починки) строк больше, проверяем только хвост.
    tail = h.split('class="code stderr-tail"', 1)[1].split("</pre>", 1)[0]
    assert "строка 39" in tail and "строка 21" in tail and "строка 20<" not in tail and "строка 20\n" not in tail
    assert "kit не найден" not in h


def test_explained_failure_keeps_journal_text(client, failed_run):
    run_id = failed_run(stderr="Traceback ...\n", events=[{"t": "log", "ts": "2026-11-14T12:00:00Z", "level": "error", "msg": "сайт показал капчу"}])
    h = client.get(f"/runs/{run_id}").text
    assert "видно в журнале ниже" in h and "stderr-tail" not in h


@pytest.mark.parametrize("kw", [{"status": "ok", "reason": None}, {"reason": "cancelled"}])
def test_no_diagnosis_for_ok_or_cancelled(home, failed_run, kw):
    run_id = failed_run(stderr=UV_KIT, **kw)
    with App(home) as app:
        assert diagnosis.diagnose(app, app.runs.get(run_id)) is None


def test_dashboard_attention_reason(client, failed_run):
    failed_run(stderr=UV_KIT)
    h = client.get("/").text
    assert "Требует внимания" in h and "kit не найден" in h
    failed_run(stderr="ModuleNotFoundError: No module named 'x'\n", run_id="20261114-140000-bbbbbb")
    h = client.get("/").text
    assert "ModuleNotFoundError: No module named" in h


def test_cli_logs_hint(home, failed_run, capsys):
    run_id = failed_run(stderr=UV_KIT)
    assert main(["logs", run_id]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith(diagnosis.KIT_HINT)
    assert main(["--json", "logs", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["hint"] == diagnosis.KIT_HINT


def test_cli_logs_mask_key_and_saved_secrets(home, failed_run, kr, capsys):
    """Агент читает `dawg-replay logs`, чтобы чинить скрипт: ключа и секретов там быть не должно."""
    kr.set_password("dawg-replay", "dawg-api-key", "SECRETKEY99")
    with App(home) as app:
        app.set_secret("ozon-cards", "sheet_token", "tok-SECRET-1")
    run_id = failed_run(
        stderr="key SECRETKEY99\ntoken tok-SECRET-1\n",
        events=[{"t": "log", "ts": "2026-11-14T12:00:00Z", "level": "info", "msg": "tok-SECRET-1 и SECRETKEY99"}],
    )
    with App(home) as app:
        (app.paths.run_dir(run_id) / "stdout.log").write_text("print SECRETKEY99\n", encoding="utf-8")
    for argv in (["logs", run_id], ["--json", "logs", run_id], ["logs", run_id, "--events"], ["--json", "logs", run_id, "--events"]):
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "SECRETKEY99" not in out and "tok-SECRET-1" not in out, argv
        assert "***" in out, argv


def test_cli_run_follow_shows_tail_and_hint(home, kr, tmp_path, monkeypatch, capsys):
    """Скрипт падает до kit: в событиях пусто, причина только в stderr."""
    import sys

    from dawg_replay import launch

    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    f = tmp_path / "boom.py"
    f.write_text(
        '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["dawg-replay-kit>=1.1,<2"]\n# ///\n\n'
        '# /// dawg-script\n# contract = "1.1"\n# name = "boom"\n# title = "Падает"\n# version = 1\n# ///\n'
        f"import sys\nsys.stderr.write({UV_KIT!r})\nsys.exit(1)\n",
        encoding="utf-8",
    )
    assert main(["run", str(f), "--follow"]) == 3
    out = capsys.readouterr().out
    assert "скрипт ничего не сообщил; последние строки stderr:" in out
    assert "was not found in the package registry" in out and out.rstrip().endswith(diagnosis.KIT_HINT)
    assert main(["--json", "run", str(f), "--follow"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["diagnosis"]["hint"] == diagnosis.KIT_HINT and data["diagnosis"]["tail"]
