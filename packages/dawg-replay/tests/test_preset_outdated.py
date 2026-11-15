"""dr-75: значения пресета, которые новая версия скрипта не принимает."""

import json
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import forms, launch
from dawg_replay.app import App
from dawg_replay.schedules import Spec
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54328
BASE = f"http://127.0.0.1:{PORT}"


def script(version, min_date):
    return f'''# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.2,<2"]
# ///

# /// dawg-script
# contract = "1.2"
# name = "orders"
# title = "Заказы"
# version = {version}
#
# [[params]]
# name = "city"
# type = "string"
# title = "Город"
# required = true
#
# [[params]]
# name = "since"
# type = "date"
# title = "С даты"
# min = "{min_date}"
# default = "{min_date}"
# ///

import json
import dawg_replay_kit as kit

(kit.output_dir / "seen.json").write_text(json.dumps({{"since": kit.params.since}}), encoding="utf-8")
kit.result("ok", "готово")
'''


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def stale(app, write_script):
    """Пресет «склады» с since = 2026-11-10 и расписание по нему; версия 2 подняла min до 2026-11-12."""
    app.registry.add(write_script(text=script(1, "2026-11-01")))
    app.save_preset("orders", "склады", {"city": "Казань", "since": "2026-11-10"})
    s = app.schedules.add("orders", "склады", Spec.daily("09:00"))
    app.registry.add(write_script(text=script(2, "2026-11-12"), filename="v2.py"))
    return s


def test_preset_explains_dropped(app, stale):
    p = app.get_preset("orders", "склады")
    assert p.dropped == ["since"] and p.params == {"city": "Казань"}
    assert p.dropped_why == {"since": "«С даты»: дата 2026-11-10 раньше min = 2026-11-12"}
    text = p.outdated()
    assert "пресет «склады» сохранён для другой версии скрипта" in text and "раньше min = 2026-11-12" in text


def test_scheduled_run_not_started(app, cli, stale):
    code, data = cli("run", "orders", "--preset", "склады", "--follow", "--schedule", stale.id)
    assert code != 0 and data["error"]["code"] == "preset_outdated"
    with App(app.paths.home) as a:
        (rec,) = a.runs.list("orders")
    assert rec.label == "failed (not_started)" and "«С даты»: дата 2026-11-10 раньше min" in rec.error


def test_manual_cli_run_warns_and_uses_default(app, cli, stale, home):
    prepared = launch.prepare(app, "orders", preset="склады")
    assert prepared.values["since"] == "2026-11-12"
    assert any("раньше min = 2026-11-12" in w and "по умолчанию" in w for w in prepared.warnings)
    app.runs.finish(prepared.run.id, status="failed", reason="cancelled")
    with pytest.raises(Exception) as e:
        launch.prepare(app, "orders", preset="склады", strict_preset=True)
    assert e.value.code == "preset_outdated"


def test_resaved_preset_runs_on_schedule(app, cli, stale, home):
    app.save_preset("orders", "склады", {"city": "Казань", "since": "2026-11-13"})
    code, data = cli("run", "orders", "--preset", "склады", "--follow", "--schedule", stale.id)
    assert code == 0, data


@pytest.fixture
def web(home, kr, app):
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": secrets_.csrf}


def test_web_form_schedule_and_quick(app, stale, web):
    c, h = web
    form = c.get("/scripts/orders/_form", params={"_preset": "склады"}).text
    assert "Пресет «склады» сохранён для другой версии скрипта" in form
    assert "«С даты»: дата 2026-11-10 раньше min = 2026-11-12" in form and 'value="2026-11-12"' in form

    block = c.get("/scripts/orders/_schedules").text
    assert "Пресет «склады» сохранён для другой версии скрипта" in block and "плановые запуски по нему не начинаются" in block

    r = c.post("/scripts/orders/quick", data={"_preset": "склады"}, headers=h)
    assert r.status_code == 200 and "Запуск «склады» не начат" in r.text and "раньше min" in r.text
    with App(app.paths.home) as a:
        assert a.runs.list("orders") == []


def test_not_started_reason_renders_code(app, cli, stale, web):
    cli("run", "orders", "--preset", "склады", "--follow", "--schedule", stale.id)
    with App(app.paths.home) as a:
        (rec,) = a.runs.list("orders")
    c, _ = web
    page = c.get(f"/runs/{rec.id}").text
    assert "Плановый запуск не начался" in page and "`" not in page.split("Плановый запуск не начался", 1)[1].split("</div>", 1)[0]
