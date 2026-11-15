"""Демо examples/telegram-demo для приёмки уведомлений: манифест и итоги без браузера."""

import json
import sys
from pathlib import Path

import pytest

from dawg_replay import launch, repair

SCRIPT = Path(__file__).resolve().parents[3] / "examples" / "telegram-demo" / "script.py"


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def demo(app):
    app.registry.add(SCRIPT)
    return app


@pytest.mark.parametrize(("outcome", "label", "summary"), [
    ("ok", "ok", "собрано 50"),
    ("partial", "partial", "3 из 5 строк, 2 упали"),
    ("failed", "failed", "сайт демо не ответил"),
    ("crash", "failed", None),
])
def test_outcomes(demo, cli, outcome, label, summary):
    code, data = cli("run", "telegram-demo", "--set", f"outcome={outcome}", "--follow")
    run = data["run"]
    assert run["label"] == label, data
    assert (code == 0) == (label in ("ok", "partial"))
    if summary:
        assert run["summary"].startswith(summary)


def test_zero_count_is_suspicious(demo, cli):
    code, data = cli("run", "telegram-demo", "--set", "count=0", "--follow")
    assert code == 0, data
    rec = demo.runs.get(data["run"]["id"])
    assert repair.suspicion(demo, rec)["kind"] == "empty"
