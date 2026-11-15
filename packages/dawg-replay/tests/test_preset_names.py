"""Имена пресетов с пробелами, кириллицей и спецсимволами: веб, CLI, расписания."""

import json
import shlex
import sys
import time

import pytest
from markupsafe import escape
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.schedules import Spec
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54325
BASE = f"http://127.0.0.1:{PORT}"
SPACED = "macbook m5 max"
CYR = "Москва (центр)"
TRICKY = 'скидки 50% & "лучшее" <a>'


def esc(text):
    """Как экранирует Jinja в шаблонах."""
    return str(escape(text))


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def c(home, kr, secrets_, write_script, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    kr.set_password("dawg-replay", "dawg-api-key", "api-KEY-1")
    with App(home, keyring_backend=kr) as a:
        a.registry.add(write_script())
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        assert client.get(f"/?token={secrets_.token}").status_code == 303
        yield client


def post(c, secrets_, url, data=None):
    return c.post(url, data=data or {}, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf})


def wait_run(home, run_id, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with App(home) as a:
            rec = a.runs.get(run_id)
        if not rec.running:
            return rec
        time.sleep(0.05)
    raise AssertionError(f"запуск {run_id} не закончился")


@pytest.mark.parametrize("name", [SPACED, CYR, TRICKY])
def test_web_save_select_chip_schedule_delete(c, secrets_, home, fake_scheduler, name):
    r = post(c, secrets_, "/scripts/ozon-cards/presets", {"_preset_name": f"  {name} ", "query": "ноутбук"})
    assert r.status_code == 200 and esc(f"Пресет «{name}» сохранён.") in r.text
    assert r.headers["HX-Trigger"] == "presets-changed"
    opt = f'<option value="{esc(name)}" selected>{esc(name)}</option>'
    assert opt in r.text

    # выбор в select подгружает значения пресета (htmx шлёт имя в query string)
    form = c.get("/scripts/ozon-cards/_form", params={"_preset": name}).text
    assert 'value="ноутбук"' in form
    # чип в библиотеке
    assert f'name="_preset" value="{esc(name)}"' in c.get("/scripts/_list").text

    r = post(c, secrets_, "/scripts/ozon-cards/schedules", {"_preset": name, "_kind": "daily", "_time": "09:00"})
    assert esc(f"пресет «{name}».") in r.text
    with App(home) as a:
        (s,) = a.schedules.all()
    assert s.preset == name
    confirm = f"Снять регулярный запуск «{s.spec.describe()}» с пресетом «{name}»?"
    assert f'hx-confirm="{esc(confirm)}"' in r.text

    line = next(x for x in (fake_scheduler / "crontab").read_text(encoding="utf-8").splitlines() if s.id in x)
    args = shlex.split(line.split(" # dawg-replay:")[0].replace("\\%", "%"))
    i = args.index("--preset")
    assert args[i + 1] == name and args[i + 2:i + 5] == ["--follow", "--schedule", s.id]
    with App(home) as a:
        assert a.schedules.sync() == {"reinstalled": [], "orphans_removed": []}
        assert a.schedules.list()["schedules"][0]["state"] == "ok"

    r = post(c, secrets_, f"/scripts/ozon-cards/schedules/{s.id}/delete")
    assert "Регулярный запуск снят" in r.text
    assert s.id not in (fake_scheduler / "crontab").read_text(encoding="utf-8")


def test_web_quick_run_and_bad_name(c, secrets_, home):
    post(c, secrets_, "/scripts/ozon-cards/presets", {"_preset_name": CYR, "query": "чайник"})
    r = post(c, secrets_, "/scripts/ozon-cards/quick", {"_preset": CYR})
    assert r.status_code == 204, r.text
    rec = wait_run(home, r.headers["hx-redirect"].rsplit("/", 1)[1])
    assert rec.preset == CYR and rec.params["query"] == "чайник"
    assert f'name="_preset" value="{esc(CYR)}"' in c.get("/").text  # теперь он в «Быстрый запуск»

    r = post(c, secrets_, "/scripts/ozon-cards/presets", {"_preset_name": "-x", "query": "чайник"})
    assert "не может начинаться с «-»" in r.text


def test_cli_spaced_preset(cli, write_script, kr, monkeypatch, fake_scheduler):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    kr.set_password("dawg-replay", "dawg-api-key", "api-KEY-1")
    assert cli("add", str(write_script()))[0] == 0
    code, data = cli("preset", "save", "ozon-cards", SPACED, "--set", "query=ноутбук")
    assert code == 0 and data["preset"]["name"] == SPACED
    code, data = cli("preset", "list", "ozon-cards")
    assert [p["name"] for p in data["presets"]] == [SPACED]

    code, data = cli("run", "ozon-cards", "--preset", SPACED, "--follow")
    assert code == 0, data
    assert data["run"]["preset"] == SPACED

    code, data = cli("schedule", "add", "ozon-cards", "--preset", SPACED, "--daily", "08:30")
    assert code == 0, data
    sid = data["schedule"]["id"]
    code, data = cli("run", "ozon-cards", "--preset", SPACED, "--follow", "--schedule", sid)
    assert code == 0, data
    assert (data["run"]["preset"], data["run"]["trigger"]) == (SPACED, "schedule")

    code, data = cli("preset", "delete", "ozon-cards", SPACED)
    assert code == 0 and data["schedules_removed"] == [sid]

    code, data = cli("preset", "save", "ozon-cards", "a\nb", "--set", "query=x")
    assert code != 0 and data["error"]["code"] == "bad_preset_name"


def test_repair_prompt_keeps_name(app, write_script):
    from dawg_replay import repair

    app.registry.add(write_script())
    app.save_preset("ozon-cards", CYR, {"query": "q"})
    app.db.execute(
        "INSERT INTO runs (id, script_name, script_path, status, reason, created_at, finished_at, preset)"
        " VALUES ('20261115-090000-aaaaaa', 'ozon-cards', 'x', 'failed', NULL, '2026-11-15T09:00:00Z', '2026-11-15T09:01:00Z', ?)",
        (CYR,),
    )
    rec = app.runs.get("20261115-090000-aaaaaa")
    assert f"пресет «{CYR}»" in repair.fix_prompt(app, rec)["text"]
