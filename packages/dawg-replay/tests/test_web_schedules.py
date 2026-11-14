import json
import re
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.schedules import Spec
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app
from dawg_replay.web.views import when_next

PORT = 54323
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def c(home, kr, secrets_, write_script, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    with App(home, keyring_backend=kr) as a:
        a.registry.add(write_script())
        a.save_preset("ozon-cards", "daily", {"query": "чайник"})
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        assert client.get(f"/?token={secrets_.token}").status_code == 303
        yield client


def post(c, secrets_, url, data=None):
    return c.post(url, data=data or {}, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf})


def test_script_page_has_block(c):
    h = c.get("/scripts/ozon-cards").text
    assert "Регулярный запуск" in h and 'hx-get="/scripts/ozon-cards/_schedules"' in h
    frag = c.get("/scripts/ozon-cards/_schedules").text
    assert 'name="_preset"' in frag and ">daily<" in frag and "Добавить расписание" in frag
    assert 'value="cron"' in frag  # фейковый планировщик cron


def test_add_list_delete(c, secrets_, fake_scheduler, home):
    r = post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": "daily", "_kind": "weekly", "_weekday": "3", "_time": "07:45"})
    assert r.status_code == 200
    assert "Расписание добавлено: по средам в 07:45" in r.text and "Удалить" in r.text
    with App(home) as a:
        (s,) = a.schedules.all()
    assert s.spec == Spec.weekly("wed", "07:45")
    assert f"# dawg-replay:{s.id}" in (fake_scheduler / "crontab").read_text(encoding="utf-8")

    # главная показывает ближайший запуск
    home_page = c.get("/").text
    assert "По расписанию" in home_page and "«daily»" in home_page

    r = post(c, secrets_, f"/scripts/ozon-cards/schedules/{s.id}/delete")
    assert "Регулярный запуск снят" in r.text
    assert (fake_scheduler / "crontab").read_text(encoding="utf-8") == ""
    assert "По расписанию" not in c.get("/").text


def test_add_errors_keep_form(c, secrets_):
    r = post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": "daily", "_kind": "daily", "_time": "25:00"})
    assert "ЧЧ:ММ" in r.text and 'value="25:00"' in r.text
    r = post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": "nope", "_kind": "daily", "_time": "09:00"})
    assert "нет пресета" in r.text
    r = post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": "daily", "_kind": "cron", "_cron": "0 0 31 2 *"})
    assert "не срабатывает" in r.text


def test_missing_task_warning(c, secrets_, fake_scheduler):
    post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": "daily", "_kind": "hours", "_hours": "6"})
    (fake_scheduler / "crontab").write_text("", encoding="utf-8")
    assert "Задачи нет в планировщике" in c.get("/scripts/ozon-cards/_schedules").text


def test_scheduler_error_shown(c, secrets_, monkeypatch):
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "1")
    frag = c.get("/scripts/ozon-cards/_schedules").text
    assert "Планировщик недоступен" in frag
    r = post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": "daily", "_kind": "daily", "_time": "09:00"})
    assert "permission denied" in r.text


def test_no_presets_hint(home, kr, secrets_, write_script, monkeypatch):
    with App(home, keyring_backend=kr) as a:
        a.registry.add(write_script())
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        client.get(f"/?token={secrets_.token}")
        frag = client.get("/scripts/ozon-cards/_schedules").text
    assert "Сохраните пресет" in frag and "Добавить расписание" not in frag


def test_delete_other_scripts_schedule_refused(c, secrets_, home):
    post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": "daily", "_kind": "daily", "_time": "09:00"})
    with App(home) as a:
        (s,) = a.schedules.all()
    r = post(c, secrets_, f"/scripts/other/schedules/{s.id}/delete")
    assert "не зарегистрирован" in r.text
    with App(home) as a:
        assert len(a.schedules.all()) == 1


def test_scheduled_run_marked_in_lists(c, home, kr):
    with App(home, keyring_backend=kr) as a:
        s = a.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
        p = launch.prepare(a, "ozon-cards", preset="daily", schedule_id=s.id)
    runs = c.get("/scripts/ozon-cards/_runs").text
    assert "по расписанию" in runs
    page = c.get(f"/runs/{p.run.id}").text
    assert "Плановый запуск" in page


def test_preset_save_refreshes_block(c, secrets_):
    r = post(c, secrets_, "/scripts/ozon-cards/presets", {"_preset_name": "night", "query": "кофе", "max_pages": "5"})
    assert r.headers.get("HX-Trigger") == "presets-changed"
    assert re.search(r'hx-trigger="presets-changed from:body"', c.get("/scripts/ozon-cards/_schedules").text)


def test_when_next():
    from datetime import datetime

    now = datetime(2026, 11, 14, 10, 0)
    assert when_next(datetime(2026, 11, 14, 12, 0), now) == "сегодня 12:00"
    assert when_next("2026-11-15T09:00", now) == "завтра 09:00"
    assert when_next(datetime(2026, 11, 16, 9, 5), now) == "пн 16 ноя 09:05"
