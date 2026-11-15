"""dr-70: секретная таблица и расписания, плановый запуск, упавший до создания запуска."""

import json
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.schedules import Spec
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

def _shared():
    """Скрипт и таблицы из test_table_params (тесты не пакет, импорт по пути)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("table_params_shared", Path(__file__).with_name("test_table_params.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CSV, module.LOGINS, module.SCRIPT


CSV, LOGINS, SCRIPT = _shared()

CITY = '''#
# [[params]]
# name = "city"
# type = "string"
# title = "Город"
# required = true
'''
PORT = 54327
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
def web(home, kr, app):
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": secrets_.csrf}


# --- секретная таблица: пресет и расписание ------------------------------------------


def test_cli_preset_warns_and_schedule_refused(acts, cli, tmp_path, fake_scheduler):
    acc, log = tmp_path / "a.csv", tmp_path / "l.csv"
    acc.write_text(CSV, encoding="utf-8")
    log.write_text(LOGINS, encoding="utf-8")
    code, data = cli("preset", "save", "acts", "демо", "--param", f"accounts=@{acc}", "--param", f"logins=@{log}")
    assert code == 0 and "logins" not in data["preset"]["params"]
    assert "Секретная таблица «Входы» в пресет не сохраняется" in data["note"] and "регулярный запуск" in data["note"]

    code, data = cli("schedule", "add", "acts", "--preset", "демо", "--daily", "09:00")
    assert code != 0 and data["error"]["code"] == "schedule_impossible"
    assert "обязательная секретная таблица «Входы»" in data["error"]["message"]
    with App(acts.paths.home) as a:
        assert a.schedules.all() == []
    assert not (fake_scheduler / "crontab").exists() or "dawg-replay" not in (fake_scheduler / "crontab").read_text(encoding="utf-8")


def test_preset_without_required_value_refused(app, write_script):
    app.registry.add(write_script())
    app.save_preset("ozon-cards", "пусто", {})
    with pytest.raises(Exception) as e:
        app.schedules.add("ozon-cards", "пусто", Spec.daily("09:00"))
    assert e.value.code == "schedule_impossible" and "нет обязательных значений: «Запрос»" in e.value.message


def test_web_preset_note_and_schedule_error(acts, web):
    c, h = web
    r = c.post("/scripts/acts/_table/accounts", files={"_table_file": ("a.csv", CSV.encode(), "text/csv")}, headers=h)
    uid = r.text.split('name="accounts" value="', 1)[1].split('"', 1)[0]
    r = c.post("/scripts/acts/presets", data={"_preset_name": "демо", "accounts": uid}, headers=h)
    assert "Пресет «демо» сохранён. Секретная таблица «Входы» в пресет не сохраняется" in r.text
    block = c.get("/scripts/acts/_schedules").text
    assert "Регулярный запуск недоступен: у скрипта обязательная секретная таблица «Входы»" in block
    assert "Добавить расписание" not in block
    r = c.post("/scripts/acts/schedules", data={"_preset": "демо", "_kind": "daily", "_time": "09:00"}, headers=h)
    assert "обязательная секретная таблица «Входы»" in r.text and "Расписание добавлено" not in r.text


def test_optional_secret_table_note_without_schedule_ban(app, tmp_path):
    f = tmp_path / "acts.py"
    f.write_text(SCRIPT, encoding="utf-8")  # logins необязательная
    app.registry.add(f)
    from dawg_replay.presets import secret_tables_note

    note = secret_tables_note(app.registry.manifest("acts"))
    assert "не сохраняется" in note and "регулярный запуск" not in note


# --- плановый запуск, не начавшийся ---------------------------------------------------


@pytest.fixture
def stale(app, write_script):
    """Расписание по пресету, а новая версия скрипта требует значение, которого в пресете нет."""
    app.registry.add(write_script())
    app.save_preset("ozon-cards", "daily", {"query": "чайник"})
    s = app.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    app.registry.add(write_script(version=2, extra_params=CITY))
    return s


def test_scheduled_run_failed_before_start_is_in_history(app, cli, stale, web):
    code, data = cli("run", "ozon-cards", "--preset", "daily", "--follow", "--schedule", stale.id)
    assert code != 0 and data["error"]["code"] == "bad_params"
    with App(app.paths.home) as a:
        (rec,) = a.runs.list("ozon-cards")
    assert (rec.label, rec.trigger, rec.schedule_id, rec.preset, rec.script_version) == (
        "failed (not_started)", "schedule", stale.id, "daily", 2)
    assert "Город" in rec.error and rec.finished_at

    c, _ = web
    home = c.get("/").text
    assert "Требует внимания" in home and "не начат" in home
    page = c.get(f"/runs/{rec.id}").text
    assert "Плановый запуск не начался" in page and "Город" in page and "Починить с агентом" not in page
    assert "ничего не сообщил" not in page and "Открыть папку" not in page
    assert "не начат" in c.get("/scripts/ozon-cards").text


def test_manual_and_orphan_failures_not_recorded(app, cli, stale):
    code, _ = cli("run", "ozon-cards", "--preset", "daily", "--follow")
    assert code != 0
    code, data = cli("run", "ozon-cards", "--preset", "daily", "--follow", "--schedule", "deadbeef")
    assert data["error"]["code"] == "schedule_not_found"
    with App(app.paths.home) as a:
        assert a.runs.list("ozon-cards") == []
