"""Экраны проектов (dr-117): список, новый, импорт, редактор (данные и сохранение), запуск, страница проекта,
«Входит в проекты» и «Добавить в проект». Без браузера, сети, системного планировщика и хранилища паролей."""

import json

import pytest
from project_scripts import SOURCE, TARGET
from starlette.testclient import TestClient

from dawg_replay import project_runs
from dawg_replay.app import App
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54321
BASE = f"http://127.0.0.1:{PORT}"

PROJECT = '''\
name = "wb-to-google"
title = "Карточки WB в Google"

[[nodes]]
id = "wb1"
script = "wb"
preset = "Магазин 1"
x = 40
y = 60

[[nodes]]
id = "google"
script = "gs"
preset = "Общая"
x = 400
y = 60

[[edges]]
from = "wb1"
to = "google"
files = [{ from = "cards.csv", to = "cards" }]
'''


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    monkeypatch.setenv(project_runs.ENV_PROCESSES, "off")
    monkeypatch.setattr(project_runs, "_spawn_step_process", lambda app, prun_id, node_id: None)


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def c(home, kr, secrets_):
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        assert client.get(f"/?token={secrets_.token}").status_code == 303
        client.csrf = secrets_.csrf
        yield client


def post(c, url, data=None, **kw):
    return c.post(url, data=data or {}, headers={"Origin": BASE, "X-CSRF-Token": c.csrf}, **kw)


def post_json(c, url, body):
    return c.post(url, content=json.dumps(body), headers={"Origin": BASE, "X-CSRF-Token": c.csrf,
                                                            "Content-Type": "application/json"})


def register(app, tmp_path, text, filename):
    f = tmp_path / filename
    f.write_text(text, encoding="utf-8")
    app.registry.add(f)


@pytest.fixture
def scripts(home, kr, tmp_path):
    with App(home, keyring_backend=kr) as app:
        register(app, tmp_path, SOURCE.format(name="wb", title="WB: карточки", columns='"sku", "name"'), "wb.py")
        register(app, tmp_path, TARGET.format(name="gs"), "gs.py")
        app.save_preset("wb", "Магазин 1", {"shop": "один", "pages": 3})
        app.save_preset("gs", "Общая", {"sheet": "Лист1", "keys": [{"_row": 2, "k": "1"}]}, {"keys": "keys.csv"})
    return home


@pytest.fixture
def project(scripts, kr, tmp_path):
    f = tmp_path / "p.project.toml"
    f.write_text(PROJECT, encoding="utf-8")
    with App(scripts, keyring_backend=kr) as app:
        app.projects.add(f)
    return "wb-to-google"


# --- список, новый, импорт ---------------------------------------------------------------------


def test_nav_and_empty_list(c):
    html = c.get("/").text
    assert 'href="/projects"' in html and ">Проекты<" in html
    html = c.get("/projects").text
    assert "Проектов пока нет" in html and 'hx-post="/projects/_new"' in html and 'hx-post="/projects/_import"' in html
    assert "<select" not in html


def test_new_project_is_a_draft_until_first_node(c, scripts, home, kr):
    r = post(c, "/projects/_new", {"title": "Карточки WB в Google"})
    assert r.status_code == 204, r.text
    url = r.headers["HX-Redirect"]
    assert url == "/projects/kartochki-wb-v-google?new=1&title=%D0%9A%D0%B0%D1%80%D1%82%D0%BE%D1%87%D0%BA%D0%B8%20WB%20%D0%B2%20Google"
    html = c.get(url).text
    assert 'data-draft="1"' in html and "Карточки WB в Google" in html and "/about" not in html
    d = c.get("/projects/kartochki-wb-v-google/_data?new=1&title=X").json()
    assert d["draft"] and d["project"]["nodes"] == [] and "wb" in d["scripts"]
    assert c.get("/projects/kartochki-wb-v-google").status_code == 404  # без new=1 черновика нет
    p = d["project"]
    p["nodes"] = [{"id": "wb", "script": "wb", "preset": "Магазин 1", "values": {}, "title": None, "x": 10, "y": 20, "wait": "all"}]
    r = post_json(c, "/projects/kartochki-wb-v-google/save", {"project": p, "base_version": None, "create": True})
    assert r.status_code == 200, r.text
    with App(home, keyring_backend=kr) as app:
        assert app.projects.get("kartochki-wb-v-google").title == "X"
    r = post(c, "/projects/_new", {"title": "X"})
    assert r.headers["HX-Redirect"].startswith("/projects/x?new=1")
    r = post(c, "/projects/_new", {"title": "Карточки WB в Google"})
    assert r.headers["HX-Redirect"].startswith("/projects/kartochki-wb-v-google-2?")  # имя занято
    assert "Назовите проект" in post(c, "/projects/_new", {"title": " "}).text


def test_list_shows_project(c, project):
    html = c.get("/projects").text
    assert "Карточки WB в Google" in html and "2 узла" in html and "без расписания" in html and "Ещё не запускался" in html


def test_import_file(c, scripts):
    r = post(c, "/projects/_import", files={"file": ("p.project.toml", PROJECT.encode(), "application/toml")})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/projects/wb-to-google?imported=added"
    r = post(c, "/projects/_import", files={"file": ("p.project.toml", PROJECT.encode(), "application/toml")})
    assert r.headers["HX-Redirect"] == "/projects/wb-to-google?imported=unchanged"
    bad = PROJECT.replace('script = "gs"', 'script = "nope"')
    r = post(c, "/projects/_import", files={"file": ("p.project.toml", bad.encode(), "application/toml")})
    assert r.status_code == 200 and "nope" in r.text and "notice-error" in r.text


# --- редактор --------------------------------------------------------------------------------------


def test_editor_page_and_data(c, project):
    html = c.get("/projects/wb-to-google").text
    assert 'data-project="wb-to-google"' in html and "/static/project.js" in html and "Запустить проект" in html
    assert "<select" not in html and "style=" not in html
    d = c.get("/projects/wb-to-google/_data").json()
    assert d["version"] == 1 and [n["id"] for n in d["project"]["nodes"]] == ["wb1", "google"]
    e = d["project"]["edges"][0]
    assert e["map"] == [{"kind": "file", "file": "cards.csv", "value": None, "column": None, "param": "cards", "rows": "first"}]
    gs = d["scripts"]["gs"]
    assert {p["name"]: p["transfer"] for p in gs["params"]}["cards"] is True
    assert gs["presets"][0]["name"] == "Общая" and "keys" in gs["presets"][0]["secret"]
    wb = d["scripts"]["wb"]
    assert [(o["name"], o["kind"], o["optional"]) for o in wb["outputs"]] == [("cards.csv", "file", False), ("report.pdf", "file", True)]
    assert wb["outputs"][0]["columns"] == ["sku", "name"]


def test_save_from_editor_makes_version(c, project):
    d = c.get("/projects/wb-to-google/_data").json()
    p = d["project"]
    p["nodes"][0]["x"] = 120
    p["nodes"][1]["values"] = {"sheet": "Новый"}
    p["edges"][0]["when"] = "ok"
    r = post_json(c, "/projects/wb-to-google/save", {"project": p, "base_version": 1})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    d2 = c.get("/projects/wb-to-google/_data").json()
    assert d2["project"]["nodes"][0]["x"] == 120 and d2["project"]["nodes"][1]["values"] == {"sheet": "Новый"}
    assert d2["project"]["edges"][0]["when"] == "ok"
    # Правка от старой версии: проект успели поменять, сохранение отказывает.
    r = post_json(c, "/projects/wb-to-google/save", {"project": p, "base_version": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "project_changed"


def test_save_rejects_errors(c, project):
    p = c.get("/projects/wb-to-google/_data").json()["project"]
    p["nodes"][0]["script"] = "nope"
    r = post_json(c, "/projects/wb-to-google/save", {"project": p, "base_version": 1})
    assert r.status_code == 400 and r.json()["error"]["problems"]
    p = c.get("/projects/wb-to-google/_data").json()["project"]
    p["name"] = "other"
    r = post_json(c, "/projects/wb-to-google/save", {"project": p, "base_version": 1})
    assert r.status_code == 400 and "Имя проекта" in r.json()["error"]["message"]


def test_check_endpoint(c, project):
    p = c.get("/projects/wb-to-google/_data").json()["project"]
    p["edges"] = []
    r = post_json(c, "/projects/_check", {"project": p})
    texts = [x["text"] for x in r.json()["problems"]]
    assert any("Карточки" in t for t in texts)  # без стрелки обязательная таблица пуста


def test_script_card(c, scripts):
    d = c.get("/projects/_script/wb").json()
    assert d["title"] == "WB: карточки" and d["builtin"] is False
    assert c.get("/projects/_script/nope").status_code == 404


# --- запуск --------------------------------------------------------------------------------------


def test_run_and_run_screen(c, project, home, kr):
    r = post(c, "/projects/wb-to-google/run")
    assert r.status_code == 204, r.text
    url = r.headers["HX-Redirect"]
    assert url.startswith("/projects/runs/p")
    prun = url.rsplit("/", 1)[1]
    html = c.get(url).text
    assert 'data-mode="run"' in html and 'id="pe-data"' in html and "<select" not in html
    st = c.get(f"{url}/_state").json()
    assert st["id"] == prun and st["status"] == "running" and set(st["steps_by_node"]) == {"wb1", "google"}
    # второй ручной запуск, пока идёт первый
    r = post(c, "/projects/wb-to-google/run")
    assert "Проект уже запущен" in r.text and url in r.text
    st = post(c, f"{url}/stop").json()
    assert st["cancel_requested"] or st["status"] != "running"


def test_run_screen_unknown(c):
    assert c.get("/projects/runs/p-nope").status_code == 404


# --- страница проекта ----------------------------------------------------------------------------------


def test_about_page(c, project):
    html = c.get("/projects/wb-to-google/about").text
    assert "Расписание" in html and "История запусков" in html and "Версии" in html and "Удалить проект" in html
    assert "<select" not in html
    assert 'name="kind"' in html and 'type="radio"' in html


def test_schedule_add_and_remove(c, project, home, kr):
    r = post(c, "/projects/wb-to-google/schedules", {"kind": "daily", "time": "03:00"})
    assert "Расписание добавлено: каждый день в 03:00" in r.text
    with App(home, keyring_backend=kr) as app:
        sid = app.schedules.project_all("wb-to-google")[0].id
    assert "каждый день в 03:00" in c.get("/projects").text
    r = post(c, "/projects/wb-to-google/schedules", {"kind": "weekly", "weekday": "9", "time": "03:00"})
    assert "field-error" in r.text
    r = post(c, f"/projects/wb-to-google/schedules/{sid}/delete")
    assert "Расписание снято" in r.text


def test_telegram_block_without_bot(c, project):
    html = c.get("/projects/wb-to-google/about").text
    assert "Telegram не подключён" in html


def test_rollback_export_delete(c, project):
    p = c.get("/projects/wb-to-google/_data").json()["project"]
    p["title"] = "Новое имя"
    assert post_json(c, "/projects/wb-to-google/save", {"project": p, "base_version": 1}).status_code == 200
    r = post(c, "/projects/wb-to-google/rollback", {"version": "1"})
    assert r.status_code == 204
    assert c.get("/projects/wb-to-google/_data").json()["project"]["title"] == "Карточки WB в Google"
    r = c.get("/projects/wb-to-google/export")
    assert r.status_code == 200 and 'name = "wb-to-google"' in r.text and "attachment" in r.headers["content-disposition"]
    r = post(c, "/projects/wb-to-google/delete")
    assert r.headers["HX-Redirect"].startswith("/projects?removed=")
    assert c.get("/projects/wb-to-google").status_code == 404


# --- страница скрипта и экран запуска скрипта ----------------------------------------------------------


def test_script_page_in_projects(c, project):
    html = c.get("/scripts/gs").text
    assert "Входит в проект: " in html and 'href="/projects/wb-to-google"' in html


def finished_run(home, kr, monkeypatch, script="wb", preset="Магазин 1"):
    import sys

    from dawg_replay import launch

    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    with App(home, keyring_backend=kr) as app:
        prepared = launch.prepare(app, script, preset=preset)
        return launch.supervise(app, prepared.run.id, prepared.payload()).id


def test_transfer_panel_offers_project(c, project, home, kr, monkeypatch):
    run_id = finished_run(home, kr, monkeypatch)
    html = c.get(f"/runs/{run_id}/_transfer?script=gs").text
    assert "добавить в проект" in html and 'hx-post="/projects/_add_from_run"' in html and "Карточки WB в Google" in html
    assert "так всегда; там же" not in html


def test_add_from_run_new_and_existing(c, project, home, kr, monkeypatch):
    run_id = finished_run(home, kr, monkeypatch)
    r = post(c, "/projects/_add_from_run", {"run_id": run_id, "target": "gs", "use_cards": "cards.csv", "use_doc": "",
                                           "project": "", "title": "WB в таблицу"})
    assert r.status_code == 204, r.text
    assert r.headers["HX-Redirect"] == "/projects/wb-v-tablitsu"
    with App(home, keyring_backend=kr) as app:
        p = app.projects.load("wb-v-tablitsu").project
        assert [(n.id, n.script, n.preset) for n in p.nodes] == [("wb", "wb", "Магазин 1"), ("gs", "gs", None)]
        assert [(f.file, f.param) for f in p.edges[0].files] == [("cards.csv", "cards")]
    r = post(c, "/projects/_add_from_run", {"run_id": run_id, "target": "gs", "use_cards": "cards.csv", "project": "wb-to-google"})
    assert r.headers["HX-Redirect"] == "/projects/wb-to-google"
    with App(home, keyring_backend=kr) as app:
        p = app.projects.load("wb-to-google").project
        assert [n.id for n in p.nodes] == ["wb1", "google", "wb", "gs"] and len(p.edges) == 2
    r = post(c, "/projects/_add_from_run", {"run_id": run_id, "target": "gs", "project": ""})
    assert "Выберите хотя бы один файл" in r.text
