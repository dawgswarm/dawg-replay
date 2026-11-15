"""dr-76: секретная таблица в пресете, расписание по такому пресету."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch, repair
from dawg_replay.app import App
from dawg_replay.schedules import Spec
from dawg_replay.uploads import Uploads
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app


def _shared():
    import importlib.util

    spec = importlib.util.spec_from_file_location("table_params_shared2", Path(__file__).with_name("test_table_params.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CSV, module.LOGINS, module.SCRIPT


CSV, LOGINS, SCRIPT = _shared()
PASSWORD = "Pa$$w0rd"
PORT = 54329
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def acts(app, tmp_path):
    f = tmp_path / "acts.py"
    f.write_text(SCRIPT.replace('# title = "Входы"\n# secret = true', '# title = "Входы"\n# required = true\n# secret = true'),
                 encoding="utf-8")
    app.registry.add(f)
    return app


@pytest.fixture
def files(tmp_path):
    acc, log = tmp_path / "Аккаунты.csv", tmp_path / "входы.csv"
    acc.write_text(CSV, encoding="utf-8")
    log.write_text(LOGINS, encoding="utf-8")
    return acc, log


def tables_dir(home):
    return Path(home) / "preset-tables" / "acts"


def leaks(home, *, skip=("seen.json",)):
    """Где в каталоге данных встречается пароль, кроме загрузок, файлов таблиц пресетов и seen.json скрипта."""
    out = []
    for f in Path(home).rglob("*"):
        if not f.is_file() or f.name in skip or "uploads" in f.parts or "preset-tables" in f.parts:
            continue
        if PASSWORD.encode() in f.read_bytes():
            out.append(str(f.relative_to(home)))
    return out


def test_cli_preset_keeps_secret_table_in_file(acts, cli, files, home):
    acc, log = files
    code, data = cli("preset", "save", "acts", "демо", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    assert code == 0, data
    assert data["preset"]["secret_tables"] == {"logins": {"file": "входы.csv", "count": 1}}
    assert "logins" not in data["preset"]["params"]
    assert data["note"] == "Таблица «Входы» (входы.csv, 1 строка) сохранена в пресете, видна только вам."
    (f,) = tables_dir(home).glob("*.json")
    assert json.loads(f.read_text(encoding="utf-8")) == [{"login": "ivan@mail.ru", "pass": PASSWORD, "_row": 2}]
    if sys.platform != "win32":
        assert f.stat().st_mode & 0o777 == 0o600 and tables_dir(home).stat().st_mode & 0o777 == 0o700
    with App(home) as a:
        row = sqlite3.connect(a.paths.db).execute("SELECT params_json FROM presets").fetchone()[0]
    assert PASSWORD not in row and "ivan@mail.ru" not in row and '"count": 1' in row

    code, data = cli("preset", "list", "acts")
    assert data["presets"][0]["secret_tables"] == {"logins": {"file": "входы.csv", "count": 1}}


def test_scheduled_run_gets_secret_table(acts, cli, files, home):
    acc, log = files
    cli("preset", "save", "acts", "демо", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    code, data = cli("schedule", "add", "acts", "--preset", "демо", "--daily", "09:00")
    assert code == 0, data
    sid = data["schedule"]["id"]
    code, data = cli("run", "acts", "--preset", "демо", "--follow", "--schedule", sid)
    assert code == 0, data
    run_id = data["run"]["id"]
    seen = json.loads((Path(home) / "runs" / run_id / "output" / "seen.json").read_text(encoding="utf-8"))
    assert seen["logins"] == [{"login": "ivan@mail.ru", "pass": PASSWORD, "_row": 2}]
    assert "logins" not in data["run"]["params"]
    assert leaks(home) == []
    with App(home) as a:
        rec = a.runs.get(run_id)
        assert PASSWORD not in repair.fix_prompt(a, rec)["text"]
        # «Перезапустить упавшие» найдёт таблицу этого запуска
        assert Uploads(a.paths.uploads).linked(run_id, "logins")["rows"][0]["login"] == "ivan@mail.ru"


def test_form_upload_overrides_preset_table_without_changing_preset(acts, cli, files, home):
    from dawg_replay import forms

    acc, log = files
    cli("preset", "save", "acts", "демо", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    built = forms.build_fields(acts, "acts", preset="демо")
    (fl,) = [f for f in built["fields"] if f.name == "logins"]
    assert fl.table["file"] == "входы.csv" and fl.table["count"] == 1 and fl.table["preview"] == []

    other = forms.upload_table(acts, "acts", "logins", b"login,pass\nanna@x.ru,other-secret\n", "другие.csv")["field"]
    (acc_fl,) = [f for f in built["fields"] if f.name == "accounts"]
    sub = forms.submit(acts, "acts", {"accounts": acc_fl.value, "logins": other.value})
    prepared = launch.prepare(acts, "acts", preset="демо", values=sub.values)
    assert prepared.values["logins"][0]["login"] == "anna@x.ru"
    p = acts.get_preset("acts", "демо")
    assert acts.presets.secret_rows(p)["logins"][0]["login"] == "ivan@mail.ru"  # пресет прежний


def test_resave_delete_and_remove_clean_files(acts, cli, files, home):
    acc, log = files
    cli("preset", "save", "acts", "демо", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    (first,) = tables_dir(home).glob("*.json")
    cli("preset", "save", "acts", "демо", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    (second,) = tables_dir(home).glob("*.json")
    assert first != second and not first.exists()
    cli("preset", "save", "acts", "второй", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    assert len(list(tables_dir(home).glob("*.json"))) == 2
    code, _ = cli("preset", "delete", "acts", "демо")
    assert code == 0 and not second.exists() and len(list(tables_dir(home).glob("*.json"))) == 1
    code, _ = cli("remove", "acts", "--yes")
    assert code == 0 and not tables_dir(home).exists()


def test_missing_table_file_means_not_started(acts, cli, files, home):
    acc, log = files
    cli("preset", "save", "acts", "демо", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    _, data = cli("schedule", "add", "acts", "--preset", "демо", "--daily", "09:00")
    for f in tables_dir(home).glob("*.json"):
        f.unlink()
    code, data2 = cli("run", "acts", "--preset", "демо", "--follow", "--schedule", data["schedule"]["id"])
    assert code != 0
    with App(home) as a:
        (rec,) = a.runs.list("acts")
    assert rec.label == "failed (not_started)" and "Входы" in rec.error


@pytest.fixture
def web(home, kr, acts):
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": secrets_.csrf}


def test_web_save_preset_with_secret_table_and_schedule(web, home):
    c, h = web
    up = {}
    for name, fname, data in (("accounts", "a.csv", CSV), ("logins", "входы.csv", LOGINS)):
        r = c.post(f"/scripts/acts/_table/{name}", files={"_table_file": (fname, data.encode(), "text/csv")}, headers=h)
        up[name] = r.text.split(f'name="{name}" value="', 1)[1].split('"', 1)[0]
    r = c.post("/scripts/acts/presets", data={"_preset_name": "демо", **up}, headers=h)
    assert "Таблица «Входы» (входы.csv, 1 строка) сохранена в пресете, видна только вам." in r.text
    assert PASSWORD not in r.text and "ivan@mail.ru" not in r.text
    form = c.get("/scripts/acts/_form", params={"_preset": "демо"}).text
    assert "<strong>входы.csv</strong>, 1 строка" in form and "Заменить файл" in form and PASSWORD not in form
    r = c.post("/scripts/acts/schedules", data={"_preset": "демо", "_kind": "daily", "_time": "09:00"}, headers=h)
    assert "Расписание добавлено" in r.text
