"""Контракт 1.2 в приложении: параметры date и table в CLI, пресетах, форме и запуске."""

import io
import json
import sys
from pathlib import Path

import pytest
from markupsafe import escape
from starlette.testclient import TestClient

from dawg_replay import forms, launch
from dawg_replay.app import App
from dawg_replay.uploads import Uploads
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

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
# max_rows = 5
#
# [[params]]
# name = "logins"
# type = "table"
# title = "Входы"
# secret = true
#
# [[params]]
# name = "since"
# type = "date"
# title = "С даты"
# min = "2026-01-01"
# default = "2026-11-01"
# ///

import json
import dawg_replay_kit as kit

p = kit.params
(kit.output_dir / "seen.json").write_text(json.dumps({
    "accounts": p.accounts, "logins": p.logins, "since": p.since,
}, ensure_ascii=False), encoding="utf-8")
if p.logins:
    kit.log.info(f"вход {p.logins[0]['login']} с паролем {p.logins[0]['pass']}")
kit.result("ok", f"строк {len(p.accounts)}")
'''

CSV = "email;Сумма\na@x.ru;10\nb@x.ru;20\n"
LOGINS = "login,pass\nivan@mail.ru,Pa$$w0rd\n"


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def acts(app, tmp_path):
    f = tmp_path / "acts.py"
    f.write_text(SCRIPT, encoding="utf-8")
    app.registry.add(f)
    return app


@pytest.fixture
def files(tmp_path):
    acc = tmp_path / "Аккаунты.csv"
    acc.write_text(CSV, encoding="utf-8")
    log = tmp_path / "входы.csv"
    log.write_text(LOGINS, encoding="utf-8")
    return acc, log


def seen(home, run_id):
    return json.loads((Path(home) / "runs" / run_id / "output" / "seen.json").read_text(encoding="utf-8"))


# --- CLI и пресеты --------------------------------------------------------------


def test_cli_preset_with_table_file(acts, cli, files):
    acc, log = files
    code, data = cli("preset", "save", "acts", "ноябрь", "--param", f"accounts=@{acc}", "--set", f"logins=@{log}",
                     "--set", "since=2026-11-10")
    assert code == 0, data
    p = data["preset"]
    assert p["params"]["accounts"] == [{"email": "a@x.ru", "Сумма": "10", "_row": 2}, {"email": "b@x.ru", "Сумма": "20", "_row": 3}]
    assert "logins" not in p["params"]  # секретная таблица в пресет не попадает
    assert p["files"] == {"accounts": "Аккаунты.csv"} and p["params"]["since"] == "2026-11-10"

    code, data = cli("preset", "list", "acts")
    assert data["presets"][0]["files"] == {"accounts": "Аккаунты.csv"}


def test_cli_text_output_shows_table_briefly(acts, files, capsys):
    from dawg_replay.cli import main

    acc, _ = files
    assert main(["preset", "save", "acts", "p", "--param", f"accounts=@{acc}"]) == 0
    out = capsys.readouterr().out
    assert '"accounts": "Аккаунты.csv, 2 строки"' in out and "a@x.ru" not in out


@pytest.mark.parametrize("value, fragment", [
    ("accounts=Аккаунты.csv", "имя=@путь"),
    ("accounts=@nope.csv", "не найден"),
    ("since=01.11.2026", "ГГГГ-ММ-ДД"),
    ("since=2025-12-31", "раньше min"),
])
def test_cli_bad_values(acts, cli, value, fragment):
    code, data = cli("preset", "save", "acts", "p", "--set", value)
    assert code != 0 and fragment in data["error"]["message"]


def test_cli_table_checked_against_manifest(acts, cli, tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("mail\nx\n", encoding="utf-8")
    code, data = cli("preset", "save", "acts", "p", "--set", f"accounts=@{f}")
    assert code != 0 and "нет колонок: «email»" in data["error"]["message"]
    f.write_text("email\n" + "\n".join(f"u{i}@x.ru" for i in range(6)) + "\n", encoding="utf-8")
    code, data = cli("preset", "save", "acts", "p", "--set", f"accounts=@{f}")
    assert code != 0 and "max_rows = 5" in data["error"]["message"]


# --- запуск -------------------------------------------------------------------------


def test_run_passes_rows_and_hides_secret_table(acts, cli, files, home, kr):
    acc, log = files
    code, data = cli("run", "acts", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}", "--follow")
    assert code == 0, data
    run = data["run"]
    got = seen(home, run["id"])
    assert got["accounts"][1] == {"email": "b@x.ru", "Сумма": "20", "_row": 3}
    assert got["logins"] == [{"login": "ivan@mail.ru", "pass": "Pa$$w0rd", "_row": 2}]
    assert got["since"] == "2026-11-01"
    # история: таблица есть, секретной нет нигде
    assert "logins" not in run["params"] and run["params"]["accounts"][0]["email"] == "a@x.ru"
    run_dir = Path(home) / "runs" / run["id"]
    for f in run_dir.rglob("*"):
        if f.is_file() and f.name != "seen.json":
            text = f.read_text(encoding="utf-8", errors="replace")
            assert "ivan@mail.ru" not in text and "Pa$$w0rd" not in text, f.name
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "вход *** с паролем ***" in events
    with App(home, keyring_backend=kr) as a:
        assert "logins" not in json.dumps(a.runs.get(run["id"]).params)
    assert not any((Path(home) / "tmp").glob("*/params.json"))  # временный файл с секретами удалён


def test_run_from_preset_with_table(acts, cli, files, home):
    acc, _ = files
    cli("preset", "save", "acts", "p", "--param", f"accounts=@{acc}")
    code, data = cli("run", "acts", "--preset", "p", "--set", "since=2026-11-14", "--follow")
    assert code == 0, data
    got = seen(home, data["run"]["id"])
    assert len(got["accounts"]) == 2 and got["since"] == "2026-11-14" and got["logins"] is None


def test_repair_prompt_shows_table_briefly(acts, cli, files, home):
    from dawg_replay import repair

    acc, _ = files
    code, data = cli("run", "acts", "--param", f"accounts=@{acc}", "--follow")
    with App(home) as a:
        text = repair.fix_prompt(a, a.runs.get(data["run"]["id"]))["text"]
    assert 'accounts = "таблица, 2 строки"' in text and "a@x.ru" not in text


# --- форма ----------------------------------------------------------------------------


def upload(app, name, data, filename):
    return forms.upload_table(app, "acts", name, data, filename)["field"]


def test_form_upload_preview_and_submit(acts):
    fl = upload(acts, "accounts", CSV.encode("cp1251"), "C:\\Users\\me\\Аккаунты.csv")
    assert fl.error is None
    t = fl.table
    assert (t["file"], t["count"], t["columns"], t["preview"]) == ("Аккаунты.csv", 2, ["email", "Сумма"], [["a@x.ru", "10"], ["b@x.ru", "20"]])
    assert fl.value == t["id"]

    sub = forms.submit(acts, "acts", {"accounts": t["id"], "since": "2026-11-20"})
    assert sub.ok, sub.errors
    assert sub.values["accounts"][0] == {"email": "a@x.ru", "Сумма": "10", "_row": 2} and sub.values["since"] == "2026-11-20"
    prepared = launch.prepare(acts, "acts", values=sub.values)
    assert prepared.run.params["accounts"][1]["email"] == "b@x.ru"


def test_form_secret_table_shows_only_name_and_count(acts):
    fl = upload(acts, "logins", LOGINS.encode(), "входы.csv")
    assert fl.table["count"] == 1 and fl.table["columns"] == [] and fl.table["preview"] == []
    acc = upload(acts, "accounts", CSV.encode(), "a.csv")
    sub = forms.save_preset(acts, "acts", "p", {"accounts": acc.table["id"], "logins": fl.table["id"]})
    assert sub.ok
    p = acts.get_preset("acts", "p")
    assert "logins" not in p.params and p.files == {"accounts": "a.csv"}
    prepared = launch.prepare(acts, "acts", values=forms.submit(acts, "acts", {"accounts": acc.table["id"], "logins": fl.table["id"]}).values)
    assert "logins" in prepared.secret_names and "logins" not in prepared.run.params
    assert prepared.values["logins"][0]["login"] == "ivan@mail.ru"  # скрипту дойдёт


@pytest.mark.parametrize("data, filename, fragment", [
    (b"mail\nx\n", "t.csv", "нет колонок: «email»"),
    (b"email\n" + b"\n".join(b"u@x.ru" for _ in range(6)), "t.csv", "строк 6, а можно не больше 5"),
    (b"email\n", "t.csv", "в таблице нет строк"),
    (b"email\n", "t.txt", "нужна таблица .csv или .xlsx"),
    (b"a,a\n1,2\n", "t.csv", "повторяется"),
])
def test_form_upload_errors(acts, data, filename, fragment):
    fl = upload(acts, "accounts", data, filename)
    assert fragment in fl.error and fl.value == ""


def test_form_errors_for_date_and_missing_table(acts):
    sub = forms.submit(acts, "acts", {"since": "2025-01-01"})
    assert sub.errors["accounts"] == "загрузите таблицу"
    assert sub.errors["since"] == "не раньше 01.01.2026"
    sub = forms.submit(acts, "acts", {"accounts": "A" * 22})
    assert "загрузите файл заново" in sub.errors["accounts"]


def test_form_upload_xlsx(acts):
    import openpyxl
    import datetime as dt

    wb = openpyxl.Workbook()
    wb.active.append(["email", "Дата", "Сумма"])
    wb.active.append(["a@x.ru", dt.date(2026, 11, 15), 12.0])
    buf = io.BytesIO()
    wb.save(buf)
    fl = upload(acts, "accounts", buf.getvalue(), "t.xlsx")
    assert fl.table["preview"] == [["a@x.ru", "2026-11-15", "12"]]


def test_preset_table_comes_back_into_form(acts):
    acc = upload(acts, "accounts", CSV.encode(), "Аккаунты.csv")
    forms.save_preset(acts, "acts", "p", {"accounts": acc.table["id"]})
    built = forms.build_fields(acts, "acts", preset="p")
    (fl,) = [f for f in built["fields"] if f.name == "accounts"]
    assert fl.table["file"] == "Аккаунты.csv" and fl.table["count"] == 2 and fl.value != acc.table["id"]
    sub = forms.submit(acts, "acts", {"accounts": fl.value})
    assert sub.ok and len(sub.values["accounts"]) == 2


def test_uploads_expire_and_are_private(acts, tmp_path):
    import time

    clock = [time.time()]
    store = Uploads(tmp_path / "up", now=lambda: clock[0])
    uid = store.put("accounts", "a.csv", [{"email": "x", "_row": 2}], secret=True)
    assert store.get(uid)["file"] == "a.csv"
    if sys.platform != "win32":
        assert (tmp_path / "up").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "up" / f"{uid}.json").stat().st_mode & 0o777 == 0o600
    clock[0] += 24 * 3600 + 1
    assert store.get(uid) is None and store.get("../etc/passwd") is None


# --- веб ---------------------------------------------------------------------------------

PORT = 54326
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def web(home, kr, acts):
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, secrets_


def test_web_form_upload_run_and_preset(web, home):
    c, s = web
    page = c.get("/scripts/acts").text
    assert 'type="date"' in page and 'min="2026-01-01"' in page and 'value="2026-11-01"' in page
    assert 'hx-post="/scripts/acts/_table/accounts"' in page and 'name="_table_file"' in page
    assert "нужны колонки «email»" in page and "не больше 5 строк" in page

    h = {"Origin": BASE, "X-CSRF-Token": s.csrf}
    r = c.post("/scripts/acts/_table/accounts", files={"_table_file": ("Аккаунты.csv", CSV.encode(), "text/csv")}, headers=h)
    assert r.status_code == 200
    assert "<strong>Аккаунты.csv</strong>, 2 строки" in r.text and "<th scope=\"col\">Сумма</th>" in r.text
    uid = r.text.split('name="accounts" value="', 1)[1].split('"', 1)[0]

    r = c.post("/scripts/acts/_table/logins", files={"_table_file": ("входы.csv", LOGINS.encode(), "text/csv")}, headers=h)
    assert "входы.csv" in r.text and "ivan@mail.ru" not in r.text and "Таблица секретная" in r.text
    secret_uid = r.text.split('name="logins" value="', 1)[1].split('"', 1)[0]

    r = c.post("/scripts/acts/presets", data={"_preset_name": "ноябрь", "accounts": uid, "logins": secret_uid}, headers=h)
    assert "Пресет «ноябрь» сохранён." in r.text and "<strong>Аккаунты.csv</strong>" in r.text
    assert "ivan@mail.ru" not in r.text
    form = c.get("/scripts/acts/_form", params={"_preset": "ноябрь"}).text
    assert "<strong>Аккаунты.csv</strong>, 2 строки" in form

    r = c.post("/scripts/acts/run", data={"accounts": uid, "logins": secret_uid, "since": "2026-11-12"}, headers=h)
    assert r.status_code == 204, r.text
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    import time

    for _ in range(400):
        with App(home) as a:
            rec = a.runs.get(run_id)
        if not rec.running:
            break
        time.sleep(0.05)
    assert rec.label == "ok", rec
    assert seen(home, run_id)["logins"][0]["pass"] == "Pa$$w0rd"


def test_web_upload_errors_keep_field(web):
    c, s = web
    h = {"Origin": BASE, "X-CSRF-Token": s.csrf}
    r = c.post("/scripts/acts/_table/accounts", files={"_table_file": ("t.csv", b"mail\nx\n", "text/csv")}, headers=h)
    assert "field-invalid" in r.text and escape("нет колонок: «email»") in r.text and 'name="_table_file"' in r.text
    r = c.post("/scripts/acts/_table/since", files={"_table_file": ("t.csv", CSV.encode(), "text/csv")}, headers=h)
    assert r.status_code == 404
    r = c.post("/scripts/acts/_table/accounts", files={"_table_file": ("t.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 403  # без CSRF не принимается
