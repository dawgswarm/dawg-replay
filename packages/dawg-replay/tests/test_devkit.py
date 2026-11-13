"""Локальный kit для разработки (dev_kit_source)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dawg_replay import devkit, launch
from dawg_replay.errors import ReplayError

KIT = Path(__file__).resolve().parents[2] / "dawg-replay-kit"
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv не установлен")


@pytest.fixture
def kit_copy(tmp_path):
    dst = tmp_path / "kit"
    shutil.copytree(KIT, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"))
    return dst


def test_without_setting_app_behaves_like_client(app, monkeypatch):
    monkeypatch.delenv(launch.ENV_SCRIPT_COMMAND, raising=False)
    assert launch.run_command(app) == (["uv", "run"], {})


def test_script_command_override_wins(app, monkeypatch):
    app.set_config("dev_kit_source", str(KIT))
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    assert launch.run_command(app) == ([sys.executable], {})


def test_bad_source(app, tmp_path):
    with pytest.raises(ReplayError) as e:
        devkit.ensure(tmp_path, app.paths.dev_kit)
    assert e.value.code == "bad_dev_kit_source"


def test_hash_ignores_caches_and_tests(kit_copy):
    before = devkit.source_hash(kit_copy)
    (kit_copy / "src" / "dawg_replay_kit" / "__pycache__").mkdir()
    (kit_copy / "src" / "dawg_replay_kit" / "__pycache__" / "x.pyc").write_bytes(b"1")
    (kit_copy / "tests").mkdir()
    (kit_copy / "tests" / "test_x.py").write_text("")
    assert devkit.source_hash(kit_copy) == before
    (kit_copy / "src" / "dawg_replay_kit" / "events.py").write_text("# правка\n", encoding="utf-8")
    assert devkit.source_hash(kit_copy) != before


@needs_uv
def test_builds_once_and_rebuilds_on_change(app, kit_copy, monkeypatch):
    monkeypatch.delenv(launch.ENV_SCRIPT_COMMAND, raising=False)
    app.set_config("dev_kit_source", str(kit_copy))
    command, env = launch.run_command(app)
    assert command == ["uv", "run", "--reinstall-package", "dawg-replay-kit"]
    first = Path(env["UV_FIND_LINKS"])
    wheels = list(first.glob("dawg_replay_kit-*.whl"))
    assert len(wheels) == 1 and wheels[0].name.startswith("dawg_replay_kit-1.")
    mtime = wheels[0].stat().st_mtime_ns
    assert launch.run_command(app)[1] == env
    assert wheels[0].stat().st_mtime_ns == mtime  # без изменений не пересобирается

    with open(kit_copy / "src" / "dawg_replay_kit" / "__init__.py", "a", encoding="utf-8") as f:
        f.write('\nDEV_MARKER = "свежий"\n')
    second = Path(launch.run_command(app)[1]["UV_FIND_LINKS"])
    assert second != first and not first.exists()


@needs_uv
def test_real_uv_run_uses_fresh_local_kit(cli, home, app, kit_copy, tmp_path, monkeypatch):
    """Живой прогон через настоящий uv run: скрипт требует kit>=1,<2, которого нет на PyPI."""
    monkeypatch.delenv(launch.ENV_SCRIPT_COMMAND, raising=False)
    with open(kit_copy / "src" / "dawg_replay_kit" / "__init__.py", "a", encoding="utf-8") as f:
        f.write('\nDEV_MARKER = "локальный"\n')
    cli("config", "set", "dev_kit_source", str(kit_copy))
    script = tmp_path / "script.py"
    script.write_text(
        """\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2"]
# ///

# /// dawg-script
# contract = 1
# name = "dev-kit-check"
# title = "Проверка локального kit"
# version = 1
# ///
import dawg_replay_kit as kit

kit.result("ok", kit.DEV_MARKER)
""",
        encoding="utf-8",
    )
    code, data = cli("run", str(script), "--follow")
    assert code == 0, data
    assert (data["run"]["label"], data["run"]["summary"]) == ("ok", "локальный")
