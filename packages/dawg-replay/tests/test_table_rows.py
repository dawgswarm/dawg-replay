"""Строки таблицы вручную в терминале: `--row таблица колонка=значение …` (dr-77)."""

import json
import sys
from pathlib import Path

import pytest

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from dawg_replay.values import parse_row_args

SCRIPT = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.2,<2"]
# ///

# /// dawg-script
# contract = "1.2"
# name = "acts"
# title = "Действия по таблице"
# version = 1
#
# [[params]]
# name = "accounts"
# type = "table"
# title = "Аккаунты"
# required = true
# columns = ["email"]
#
# [[params]]
# name = "logins"
# type = "table"
# title = "Входы"
# secret = true
# ///

import json
import dawg_replay_kit as kit

(kit.output_dir / "seen.json").write_text(json.dumps({
    "accounts": kit.params.accounts, "logins": kit.params.logins,
}, ensure_ascii=False), encoding="utf-8")
kit.log.info(f"пароль {kit.params.logins[0]['pass'] if kit.params.logins else ''}")
'''


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def acts(app, tmp_path):
    f = tmp_path / "acts.py"
    f.write_text(SCRIPT, encoding="utf-8")
    app.registry.add(f)
    return app


def seen(home, run_id):
    return json.loads((Path(home) / "runs" / run_id / "output" / "seen.json").read_text(encoding="utf-8"))


def test_parse_rows_numbering_and_hidden(acts):
    m = acts.registry.manifest("acts")
    asked = []

    def hidden(prompt):
        asked.append(prompt)
        return "Pa$$"

    out = parse_row_args(m, [["accounts", "email=a@x.ru", "city=Москва"], ["logins", "login=ivan", "pass=-"],
                             ["accounts", "email=b@x.ru=c"]], hidden)
    assert out["accounts"] == [{"email": "a@x.ru", "city": "Москва", "_row": 2}, {"email": "b@x.ru=c", "_row": 3}]
    assert out["logins"] == [{"login": "ivan", "pass": "Pa$$", "_row": 2}]
    assert asked == ["logins, строка 2, pass: "]


@pytest.mark.parametrize("rows, sets, fragment", [
    ([["nope", "a=1"]], (), "такой таблицы у скрипта нет (таблицы: accounts, logins)"),
    ([["accounts", "email"]], (), "колонка=значение"),
    ([["accounts", "_row=5"]], (), "не может начинаться с «_»"),
    ([["accounts"]], (), "нет ни одной ячейки"),
    ([["accounts", "email=a"]], ("accounts=@f.csv",), "и файлом (--set), и строками (--row)"),
])
def test_parse_rows_errors(acts, rows, sets, fragment):
    with pytest.raises(ReplayError) as e:
        parse_row_args(acts.registry.manifest("acts"), rows, lambda p: "", sets)
    assert fragment in e.value.message


def test_cli_run_with_rows_and_hidden_secret(acts, cli, home):
    code, data = cli("run", "acts", "--row", "accounts", "email=a@x.ru", "--row", "accounts", "email=b@x.ru",
                     "--row", "logins", "login=ivan", "pass=-", "--follow", stdin="Pa$$w0rd\n")
    assert code == 0, data
    rid = data["run"]["id"]
    got = seen(home, rid)
    assert got["accounts"] == [{"email": "a@x.ru", "_row": 2}, {"email": "b@x.ru", "_row": 3}]
    assert got["logins"] == [{"login": "ivan", "pass": "Pa$$w0rd", "_row": 2}]
    with App(home) as a:
        rec = a.runs.get(rid)
        assert "logins" not in rec.params  # секретная таблица не в истории
        run_dir = a.paths.run_dir(rid)
        for f in run_dir.rglob("*"):
            if f.is_file() and f.name != "seen.json":
                assert "Pa$$w0rd" not in f.read_text("utf-8", errors="replace"), f


def test_cli_rows_checked_against_columns(acts, cli):
    code, data = cli("run", "acts", "--row", "accounts", "mail=a@x.ru")
    assert code == 1 and "email" in data["error"]["message"]


def test_cli_preset_save_with_rows(acts, cli):
    code, data = cli("preset", "save", "acts", "один", "--row", "accounts", "email=a@x.ru",
                     "--row", "logins", "login=ivan", "pass=x")
    assert code == 0, data
    assert data["preset"]["params"] == {"accounts": [{"email": "a@x.ru", "_row": 2}]}  # секретная не сохраняется
    assert "note" in data


def test_visible_secret_values_warn(acts, capsys):
    from dawg_replay.cli import main

    code = main(["run", "acts", "--row", "accounts", "email=a@x.ru", "--row", "logins", "login=ivan", "pass=open",
                 "--follow"])
    assert code == 0
    err = capsys.readouterr().err
    assert "таблица `logins` секретная" in err and "колонка=-" in err
    assert "open" not in err  # само значение в предупреждении не печатается


def test_hidden_secret_values_do_not_warn(acts, capsys, monkeypatch):
    import io

    from dawg_replay.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO("ivan\nPa$$\n"))
    code = main(["run", "acts", "--row", "accounts", "email=a@x.ru", "--row", "logins", "login=-", "pass=-", "--follow"])
    err = capsys.readouterr().err
    assert code == 0, err
    assert "секретная" not in err and "Pa$$" not in err
