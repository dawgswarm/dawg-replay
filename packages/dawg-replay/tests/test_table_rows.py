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
    assert "таблица `logins` секретная, а значения колонок «login», «pass» набраны" in err
    assert "задайте login=- pass=-" in err
    assert "open" not in err  # само значение в предупреждении не печатается


def test_visible_secret_warning_names_only_typed_columns(acts, capsys, monkeypatch):
    import io

    from dawg_replay.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO("Pa$$\n"))
    code = main(["run", "acts", "--row", "accounts", "email=a@x.ru", "--row", "logins", "login=ivan", "pass=-", "--follow"])
    err = capsys.readouterr().err
    assert code == 0, err
    assert "значение колонки «login» набрано в команде" in err and "задайте login=-" in err
    assert "«pass»" not in err and "Pa$$" not in err and "ivan" not in err


def test_hidden_secret_values_do_not_warn(acts, capsys, monkeypatch):
    import io

    from dawg_replay.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO("ivan\nPa$$\n"))
    code = main(["run", "acts", "--row", "accounts", "email=a@x.ru", "--row", "logins", "login=-", "pass=-", "--follow"])
    err = capsys.readouterr().err
    assert code == 0, err
    assert "секретная" not in err and "Pa$$" not in err


# --- форма: «Ввести вручную» ---

from starlette.testclient import TestClient  # noqa: E402

from dawg_replay import forms  # noqa: E402
from dawg_replay.uploads import Uploads  # noqa: E402
from dawg_replay.web.security import Secrets  # noqa: E402
from dawg_replay.web.server import create_app  # noqa: E402

PORT = 54337
BASE = f"http://127.0.0.1:{PORT}"


def test_manual_table_rows_and_upload(acts):
    fl = forms.manual_table(acts, "acts", "accounts", ["email", "city"],
                            ["a@x.ru", "Москва", "", "", " b@x.ru ", ""])["field"]
    assert fl.error is None and fl.table["file"] == "введено вручную" and fl.table["count"] == 2
    data = Uploads(acts.paths.uploads).get(fl.table["id"])
    assert data["rows"] == [{"email": "a@x.ru", "city": "Москва", "_row": 2}, {"email": "b@x.ru", "city": "", "_row": 3}]
    assert data["secret"] is False


@pytest.mark.parametrize("cols, cells, fragment", [
    ([], [], "хотя бы одну колонку"),
    (["email", ""], ["a", "b"], "у каждой колонки нужно имя"),
    (["email", "email"], ["a", "b"], "повторяются"),
    (["_row"], ["2"], "не может начинаться"),
    (["email"], ["", ""], "хотя бы одну строку"),
    (["mail"], ["a@x.ru"], "email"),
    (["email", "x"], ["a"], "неполную строку"),
])
def test_manual_table_errors(acts, cols, cells, fragment):
    fl = forms.manual_table(acts, "acts", "accounts", cols, cells)["field"]
    assert fl.error and fragment in fl.error, fl.error
    assert fl.table["failed"] and fl.manual["open"]


@pytest.fixture
def web(home, kr, acts):
    s = Secrets(port=PORT)
    with TestClient(create_app(home, s, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={s.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": s.csrf}


def _uid(html, name):
    return html.split(f'name="{name}" value="', 1)[1].split('"', 1)[0]


def test_web_manual_entry_run_and_preset(web, home):
    c, h = web
    page = c.get("/scripts/acts").text
    assert "Ввести вручную" in page and 'data-url="/scripts/acts/_table/accounts/manual"' in page
    assert '<span data-col="email">email</span>' in page  # колонки из columns манифеста
    # секретная таблица без columns: имена колонок задаёт человек
    assert 'data-secret="1"' in page and "data-col-input" in page and "+ колонка" in page

    r = c.post("/scripts/acts/_table/accounts/manual", data={"_cols": ["email"], "_cells": ["a@x.ru", "b@x.ru"]}, headers=h)
    assert r.status_code == 200 and "<strong>введено вручную</strong>, 2 строки" in r.text
    acc = _uid(r.text, "accounts")
    r = c.post("/scripts/acts/_table/logins/manual", data={"_cols": ["login", "pass"], "_cells": ["ivan", "Pa$$w0rd"]}, headers=h)
    assert "введено вручную" in r.text and "Pa$$w0rd" not in r.text and "ivan" not in r.text
    log = _uid(r.text, "logins")

    r = c.post("/scripts/acts/run", data={"accounts": acc, "logins": log}, headers=h)
    assert r.status_code == 204, r.text
    rid = r.headers["hx-redirect"].rsplit("/", 1)[1]
    for _ in range(400):
        with App(home) as a:
            rec = a.runs.get(rid)
        if not rec.running:
            break
        import time
        time.sleep(0.05)
    got = seen(home, rid)
    assert got["accounts"] == [{"email": "a@x.ru", "_row": 2}, {"email": "b@x.ru", "_row": 3}]
    assert got["logins"] == [{"login": "ivan", "pass": "Pa$$w0rd", "_row": 2}]

    r = c.post("/scripts/acts/presets", data={"_preset_name": "руками", "accounts": acc, "logins": log}, headers=h)
    assert "Пресет «руками» сохранён." in r.text
    with App(home) as a:
        p = a.get_preset("acts", "руками")
        assert p.params["accounts"] == [{"email": "a@x.ru", "_row": 2}, {"email": "b@x.ru", "_row": 3}]
        assert p.files["accounts"] == "введено вручную"
        assert a.presets.secret_rows(p)["logins"] == [{"login": "ivan", "pass": "Pa$$w0rd", "_row": 2}]


def test_web_manual_error_keeps_input_but_not_secret_values(web):
    c, h = web
    r = c.post("/scripts/acts/_table/logins/manual", data={"_cols": ["login", "pass", "login"],
                                                          "_cells": ["ivan", "Pa$$w0rd", "x"]}, headers=h)
    assert "имена колонок повторяются" in r.text
    assert 'value="ivan"' in r.text and "Pa$$w0rd" not in r.text  # пароль заново, логин остался
    assert "<div class=\"table-manual\"" in r.text and " hidden>" not in r.text.split('class="table-manual"', 1)[1].split(">", 1)[0] + ">"


def test_web_manual_needs_csrf(web):
    c, _ = web
    r = c.post("/scripts/acts/_table/accounts/manual", data={"_cols": ["email"], "_cells": ["a"]}, headers={"Origin": BASE})
    assert r.status_code == 403



# --- пустые колонки из columns (dr-81): одно правило для файла, ввода вручную и --row ---


def test_empty_required_column_in_manual_row(acts):
    fl = forms.manual_table(acts, "acts", "accounts", ["email", "city"], ["a@x.ru", "", "", "Москва"])["field"]
    assert fl.error == "строка 2: пустая колонка «email»"  # номер как в редакторе, не как в файле
    fl = forms.manual_table(acts, "acts", "accounts", ["email", "city"], ["a@x.ru", "", "", "", "", "Тула"])["field"]
    assert fl.error == "строка 3: пустая колонка «email»"  # пустая вторая строка в редакторе тоже считается


def test_empty_required_column_in_file(acts):
    fl = forms.upload_table(acts, "acts", "accounts", "email;city\na@x.ru;x\n  ;Казань\n".encode(), "a.csv")["field"]
    assert fl.error == "строка 3: пустая колонка «email»"


def test_empty_required_column_in_cli_rows(acts, cli):
    code, data = cli("run", "acts", "--row", "accounts", "email=a@x.ru", "--row", "accounts", "city=Тула")
    assert code == 1 and "строка 3: пустая колонка «email»" in data["error"]["message"]
