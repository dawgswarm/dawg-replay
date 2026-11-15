"""Блоки правил Telegram в UI: Настройки, страница скрипта, расписание, выбор на форме запуска."""

import json
import re
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch, tg_rules
from dawg_replay.app import App
from dawg_replay.schedules import Spec
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54331
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def connected(monkeypatch):
    state = {"on": True}
    monkeypatch.setattr(tg_rules, "connected", lambda app: state["on"])
    return state


@pytest.fixture
def c(home, kr, secrets_, write_script, monkeypatch, connected):
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


def test_general_rules_form(c, secrets_, home, kr):
    r = post(c, secrets_, "/settings/telegram/rules",
             {"failed": "on", "done_manual": "always", "long_minutes": "15", "attach_main": "on"})
    assert r.status_code == 200 and "Сохранено." in r.text
    with App(home, keyring_backend=kr) as a:
        rules = tg_rules.general(a.config)
    # галочка без отметки значит «не писать»
    assert rules == {"failed": True, "ask": False, "done_scheduled": False, "done_manual": "always",
                     "long_minutes": 15, "suspicious": False, "attach_main": True}
    assert re.search(r'name="attach_main" value="on"\s+checked', r.text)
    r = post(c, secrets_, "/settings/telegram/rules", {"done_manual": "long", "long_minutes": "0"})
    assert "notice-error" in r.text
    with App(home, keyring_backend=kr) as a:
        assert a.config.telegram_done_manual == "always"  # ошибка: ничего не сохранено


def test_settings_block_has_rules_only_when_connected(c, home, kr):
    assert 'id="tg-rules"' not in c.get("/settings").text
    with App(home, keyring_backend=kr) as a:
        a.secrets.set_telegram_token("123456:" + "A" * 30)
        a.set_config("telegram_chat_id", "777")
        a.set_config("telegram_bot", "my_bot")
    page = c.get("/settings").text
    assert 'id="tg-rules"' in page and 'hx-post="/settings/telegram/rules"' in page and "О чём писать" in page


def test_script_block_and_overrides(c, secrets_, home, kr, connected):
    page = c.get("/scripts/ozon-cards").text
    assert "Уведомления в Telegram" in page and "как в общих настройках" in page
    assert 'hx-post="/scripts/ozon-cards/telegram"' in page and "как в общих: писать" in page
    r = post(c, secrets_, "/scripts/ozon-cards/telegram", {"failed": "off", "long_minutes": "30", "ask": "inherit"})
    assert "Сохранено." in r.text
    with App(home, keyring_backend=kr) as a:
        assert tg_rules.overrides(a, "script", "ozon-cards") == {"failed": False, "long_minutes": 30}
    assert "свои правила у скрипта" in c.get("/scripts/ozon-cards").text
    r = post(c, secrets_, "/scripts/ozon-cards/telegram", {"done_manual": "sometimes"})
    assert "notice-error" in r.text
    connected["on"] = False
    page = c.get("/scripts/ozon-cards").text
    assert "Подключить в Настройках" in page and 'hx-post="/scripts/ozon-cards/telegram"' not in page


def test_schedule_block(c, secrets_, home, kr, connected):
    with App(home, keyring_backend=kr) as a:
        s = a.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
        tg_rules.set_overrides(a, "script", "ozon-cards", {"failed": "off"})
    frag = c.get("/scripts/ozon-cards/_schedules").text
    assert "Уведомления в Telegram" in frag and "как у скрипта: не писать" in frag
    assert 'name="done_manual"' not in frag.split("schedule-tg", 1)[1].split("</details>", 1)[0]
    r = post(c, secrets_, f"/scripts/ozon-cards/schedules/{s.id}/telegram", {"failed": "on"})
    assert "Сохранено." in r.text
    with App(home, keyring_backend=kr) as a:
        assert tg_rules.effective(a, "ozon-cards", s.id)["failed"] is True
    r = post(c, secrets_, "/scripts/other/schedules/" + s.id + "/telegram", {"failed": "on"})
    assert "другого скрипта" in r.text or "нет" in r.text
    connected["on"] = False
    assert "Уведомления в Telegram" not in c.get("/scripts/ozon-cards/_schedules").text


def test_run_form_notify_choice(c, secrets_, home, kr, connected):
    form = c.get("/scripts/ozon-cards").text
    assert 'name="_notify"' in form and "по настройкам: если дольше 10 мин" in form
    r = post(c, secrets_, "/scripts/ozon-cards/run", {"_preset": "", "query": "чайник", "_notify": "always"})
    assert r.status_code == 204
    run_id = r.headers["HX-Redirect"].rsplit("/", 1)[1]
    with App(home, keyring_backend=kr) as a:
        assert a.runs.get(run_id).notify == "always"
    # ошибка в форме сохраняет выбор
    r = post(c, secrets_, "/scripts/ozon-cards/run", {"_preset": "", "query": "", "_notify": "never"})
    assert re.search(r'<option value="never"\s+selected', r.text)
    connected["on"] = False
    assert 'name="_notify"' not in c.get("/scripts/ozon-cards").text
