"""Контракт 1.3 в приложении: подписи enum, multi_enum, time, datetime, относительные даты."""

import datetime as dt
import json
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dawg_replay import forms, launch
from dawg_replay.app import App
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

SCRIPT = '''# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "m4"
# title = "Отчёт"
# version = 1
#
# [[params]]
# name = "sort"
# type = "enum"
# title = "Сортировка"
# default = "popular"
# choices = [{ value = "popular", title = "Популярные" }, { value = "price", title = "Дешевле" }]
#
# [[params]]
# name = "stores"
# type = "multi_enum"
# title = "Склады"
# choices = [{ value = "msk", title = "Москва" }, { value = "kzn", title = "Казань" }, { value = "spb", title = "Петербург" }]
# min_items = 1
# default = ["msk"]
#
# [[params]]
# name = "at"
# type = "time"
# title = "Время"
# default = "09:00"
#
# [[params]]
# name = "since"
# type = "date"
# title = "С даты"
# min = "2020-01-01"
# default = "today-1"
#
# [[params]]
# name = "moment"
# type = "datetime"
# title = "Момент"
# default = "month_startT08:30"
# ///

import json
import dawg_replay_kit as kit

p = kit.params
(kit.output_dir / "seen.json").write_text(json.dumps(p.to_dict(), ensure_ascii=False), encoding="utf-8")
kit.result("ok", "готово")
'''

TODAY = dt.date.today()
YESTERDAY = (TODAY - dt.timedelta(days=1)).isoformat()
MONTH_START = TODAY.replace(day=1).isoformat()
PORT = 54330
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def m4(app, tmp_path):
    f = tmp_path / "m4.py"
    f.write_text(SCRIPT, encoding="utf-8")
    app.registry.add(f)
    return app


def seen(home, run_id):
    return json.loads((Path(home) / "runs" / run_id / "output" / "seen.json").read_text(encoding="utf-8"))


def test_cli_defaults_and_relative_preset(m4, cli, home):
    code, data = cli("run", "m4", "--follow")
    assert code == 0, data
    got = seen(home, data["run"]["id"])
    assert got == {"sort": "popular", "stores": ["msk"], "at": "09:00", "since": YESTERDAY, "moment": f"{MONTH_START}T08:30"}
    assert data["run"]["params"]["since"] == YESTERDAY  # в истории фактическая дата

    code, data = cli("preset", "save", "m4", "отчёт", "--set", "stores=kzn,spb", "--set", "sort=price",
                     "--set", "since=prev_month_start", "--set", "moment=today-1T18:00", "--set", "at=07:45")
    assert code == 0, data
    assert data["preset"]["params"]["since"] == "prev_month_start"  # пресет хранит выражение
    code, data = cli("preset", "save", "m4", "плохо", "--set", "stores=msk,nsk")
    assert code != 0 and "нет в choices" in data["error"]["message"]

    code, data = cli("schedule", "add", "m4", "--preset", "отчёт", "--daily", "09:00")
    sid = data["schedule"]["id"]
    code, data = cli("run", "m4", "--preset", "отчёт", "--follow", "--schedule", sid)
    assert code == 0, data
    got = seen(home, data["run"]["id"])
    prev_start = (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=1).isoformat()
    assert got["since"] == prev_start and got["moment"] == f"{YESTERDAY}T18:00"
    assert got["stores"] == ["kzn", "spb"] and got["sort"] == "price" and got["at"] == "07:45"


def test_form_parse_relative_and_choices(m4):
    sub = forms.submit(m4, "m4", {"stores": ["msk", "spb"], "since__rel": "today-1", "since": "",
                                  "moment__rel": "week_start", "moment__reltime": "10:00", "at": "11:30", "sort": "price"})
    assert sub.ok, sub.errors
    assert sub.values == {"stores": ["msk", "spb"], "since": "today-1", "moment": "week_startT10:00", "at": "11:30", "sort": "price"}
    sub = forms.submit(m4, "m4", {"since": "2026-11-03", "since__rel": ""})
    assert sub.ok and sub.values["since"] == "2026-11-03"
    sub = forms.submit(m4, "m4", {"at": "25:00", "since": "2019-12-31"})
    assert sub.errors["at"] == "нужно время ЧЧ:ММ" and sub.errors["since"] == "не раньше 01.01.2020"
    sub = forms.submit(m4, "m4", {"since__rel": "nonsense"})
    assert sub.errors["since"] == "выберите вариант из списка"


def test_relative_beyond_min_explained(app, tmp_path):
    f = tmp_path / "m.py"
    f.write_text(SCRIPT.replace('# min = "2020-01-01"', f'# min = "{TODAY.isoformat()}"').replace(
        '# default = "today-1"', '# default = "today"'), encoding="utf-8")
    app.registry.add(f)
    sub = forms.submit(app, "m4", {"since__rel": "today-1"})
    assert sub.errors["since"].startswith("вчера сегодня даёт") and "не раньше" in sub.errors["since"]


def test_preset_relative_shown_in_form(m4):
    sub = forms.save_preset(m4, "m4", "п", {"since__rel": "month_end", "moment__rel": "today", "stores": ["kzn"]})
    assert sub.ok, sub.errors
    built = forms.build_fields(m4, "m4", preset="п")
    by = {f.name: f for f in built["fields"]}
    assert by["since"].relative == "month_end" and by["since"].value == ""
    assert by["moment"].relative == "today" and by["stores"].value == ["kzn"]
    assert by["sort"].choice_titles == [("popular", "Популярные"), ("price", "Дешевле")]


@pytest.fixture
def web(home, kr, m4):
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": secrets_.csrf}


def test_web_form_and_run_page(web, home):
    c, h = web
    page = c.get("/scripts/m4").text
    assert ">Дешевле</option>" in page and ">Популярные</option>" in page
    assert 'type="checkbox" value="kzn"' in page and "<span>Казань</span>" in page
    assert 'type="time"' in page and 'type="datetime-local"' in page
    y_ru = (TODAY - dt.timedelta(days=1)).strftime("%d.%m.%Y")
    assert f'<option value="today-1" selected>вчера: {y_ru}</option>' in page and "сегодня это" not in page
    assert "по умолчанию вчера" in page.lower()
    r = c.post("/scripts/m4/run", data={"sort": "price", "stores": ["msk", "kzn"], "at": "09:00", "since__rel": "today-1",
                                        "moment": "2026-11-20T18:00"}, headers=h)
    assert r.status_code == 204, r.text
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    for _ in range(400):
        with App(home) as a:
            if not a.runs.get(run_id).running:
                break
        time.sleep(0.05)
    run = c.get(f"/runs/{run_id}").text
    assert "Параметры запуска" in run and "<dd>Дешевле</dd>" in run and "<dd>Москва, Казань</dd>" in run
    y = TODAY - dt.timedelta(days=1)
    assert f"<dd>{y.strftime('%d.%m.%Y')}, из выражения «вчера»</dd>" in run and "<dd>20.11.2026 18:00</dd>" in run
    # «Запустить снова» предупреждает, что дата не посчитается заново
    assert f"«С даты» {y.strftime('%d.%m.%Y')} из выражения «вчера»" in run and "hx-confirm=" in run


def test_cli_accepts_choice_titles(m4, cli):
    code, data = cli("preset", "save", "m4", "п", "--set", "sort=Дешевле", "--set", "stores=Москва,kzn")
    assert code == 0, data
    assert data["preset"]["params"]["sort"] == "price" and data["preset"]["params"]["stores"] == ["msk", "kzn"]
    code, data = cli("preset", "save", "m4", "п2", "--set", "sort=Дорогие")
    assert code != 0 and "нет в choices: popular (Популярные), price (Дешевле)" in data["error"]["message"]


def test_home_attention_again_confirms_relative_date(app, tmp_path, cli, web):
    f = tmp_path / "fail.py"
    f.write_text(SCRIPT.replace('# name = "m4"', '# name = "m4-fail"').replace(
        'kit.result("ok", "готово")', 'kit.result("failed", "сайт не ответил")\nraise SystemExit(1)'), encoding="utf-8")
    app.registry.add(f)
    code, data = cli("run", "m4-fail", "--follow")
    assert code != 0 and data["run"]["label"] == "failed"
    c, _ = web
    home = c.get("/").text
    y = (TODAY - dt.timedelta(days=1)).strftime("%d.%m.%Y")
    assert "Требует внимания" in home
    assert f'hx-confirm="Повтор возьмёт те же значения: «С даты» {y} из выражения «вчера»' in home
