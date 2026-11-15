import json
import subprocess
import sys

import pytest

from dawg_replay import __version__
from dawg_replay.cli import main

GOOD = """\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2"]
# ///

# /// dawg-script
# contract = 1
# name = "demo"
# title = "Демо"
# version = 2
#
# [[params]]
# name = "query"
# type = "string"
# title = "Запрос"
# required = true
# ///
"""

BAD = GOOD.replace("version = 2", "version = 0").replace('type = "string"', 'type = "timestamp"')


@pytest.fixture
def script(tmp_path):
    def write(text):
        f = tmp_path / "script.py"
        f.write_text(text, encoding="utf-8")
        return str(f)

    return write


def test_version(capsys):
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert f"dawg-replay {__version__}" in out
    assert "контракт скриптов: 1.3" in out


def test_version_json(capsys):
    assert main(["--version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"version": __version__, "contracts": [1], "contract_versions": ["1.3"]}


def test_validate_ok(script, capsys):
    path = script(GOOD)
    assert main(["validate", path]) == 0
    out = capsys.readouterr().out
    assert "ок" in out and "demo v2" in out and "query (string)" in out


def test_validate_ok_json(script, capsys):
    path = script(GOOD)
    assert main(["validate", path, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["errors"] == []
    assert data["manifest"]["name"] == "demo"
    assert data["manifest"]["params"][0]["required"] is True


def test_validate_errors(script, capsys):
    path = script(BAD)
    assert main(["validate", path]) == 1
    err = capsys.readouterr().err
    assert "ошибок: 2" in err
    assert "version:" in err
    assert "params[0](query).type: неизвестный тип 'timestamp'" in err


def test_validate_errors_json_flag_before_command(script, capsys):
    path = script(BAD)
    assert main(["--json", "validate", path]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert [e["code"] for e in data["errors"]] == ["bad_value", "bad_type"]
    assert set(data["errors"][0]) == {"code", "path", "message", "line"}


def test_validate_missing_file(tmp_path, capsys):
    assert main(["validate", str(tmp_path / "nope.py"), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "file_not_found"


def test_entry_point_runs(script):
    path = script(GOOD)
    r = subprocess.run(
        [sys.executable, "-m", "dawg_replay", "validate", path, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["ok"] is True


def test_validate_file_with_bom(tmp_path, capsys):
    f = tmp_path / "script.py"
    f.write_bytes(GOOD.encode("utf-8-sig"))
    assert main(["validate", str(f)]) == 0
