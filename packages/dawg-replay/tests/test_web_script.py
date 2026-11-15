import json
import re
import sys
import time

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54322
BASE = f"http://127.0.0.1:{PORT}"

ALL_TYPES = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

# /// dawg-script
# contract = 1
# name = "all-types"
# title = "Все типы"
# version = {version}
#
# [[params]]
# name = "query"
# type = "string"
# title = "Запрос"
# required = true
{notes}#
# [[params]]
# name = "max_pages"
# type = "int"
# title = "Страницы"
# default = 5
# min = 1
# max = 50
#
# [[params]]
# name = "min_rating"
# type = "float"
# title = "Рейтинг"
# min = 0
# max = 5
#
# [[params]]
# name = "in_stock"
# type = "bool"
# title = "В наличии"
# default = true
#
# [[params]]
# name = "sort"
# type = "enum"
# title = "Сортировка"
# choices = ["popular", "price"]
# default = "popular"
#
# [[params]]
# name = "start_url"
# type = "url"
# title = "Старт"
#
# [[params]]
# name = "categories"
# type = "url_list"
# title = "Категории"
# max_items = 2
#
# [[params]]
# name = "brands"
# type = "string_list"
# title = "Бренды"
#
# [[params]]
# name = "token"
# type = "secret"
# title = "Токен"
# ///
# <script>alert("xss")</script> & прочее
import dawg_replay_kit as kit
kit.result("ok", "готово")
'''

NOTES = '''#
# [[params]]
# name = "notes"
# type = "text"
# title = "Заметки"
'''


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def script(home, tmp_path):
    def add(version=1, notes=True):
        f = tmp_path / f"all-types-v{version}.py"
        f.write_text(ALL_TYPES.format(version=version, notes=NOTES if notes else ""), encoding="utf-8")
        with App(home) as app:
            app.registry.add(f)
        return f

    add()
    return add


@pytest.fixture
def c(home, kr, secrets_, script, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        assert client.get(f"/?token={secrets_.token}").status_code == 303
        yield client


def post(c, secrets_, url, data=None):
    return c.post(url, data=data or {}, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf})


def runs_count(home):
    with App(home) as app:
        return len(app.runs.list("all-types", 100))


VALID = {
    "query": "ноутбук",
    "notes": "строка 1\r\nстрока 2",
    "max_pages": "3",
    "min_rating": "4,5",
    "in_stock": "on",
    "sort": "price",
    "start_url": "https://www.ozon.ru/",
    "categories": "https://a.ru/1\r\n\r\nhttps://a.ru/2",
    "brands": "Apple\r\nAsus",
}


# --- рендер ---


def test_renders_all_types(c):
    html = c.get("/scripts/all-types").text
    assert '<input id="p-query" name="query" type="text" value=""' in html
    assert '<textarea id="p-notes" name="notes"' in html
    assert re.search(r'id="p-max_pages" name="max_pages" type="number"[^>]*step="1" value="5"\s+min="1" max="50"', html)
    assert re.search(r'id="p-min_rating" name="min_rating" type="number"[^>]*step="any" value=""\s+min="0" max="5"', html)
    assert re.search(r'id="p-in_stock" name="in_stock" type="checkbox" value="on" checked', html)
    assert '<option value="popular" selected>popular</option>' in html and '<option value="price" >price</option>' in html
    assert 'name="start_url" type="url"' in html
    assert '<textarea id="p-categories" name="categories"' in html and "не больше 2" in html
    assert '<textarea id="p-brands" name="brands"' in html and "По одному значению на строку" in html
    assert 'name="token" type="password"' in html and "Секрет не задан" in html
    assert "обязательное" in html and "От 1 до 50, по умолчанию 5." in html
    assert "Скрипт ещё не запускался" in html
    assert "Версия 1" in html and "текущая" in html


def test_code_is_escaped(c):
    html = c.get("/scripts/all-types").text
    assert "&lt;script&gt;alert(&#34;xss&#34;)&lt;/script&gt; &amp; прочее" in html
    assert '<script>alert("xss")' not in html


def test_unknown_script_404(c):
    assert c.get("/scripts/nope").status_code == 404


# --- запуск ---


def wait_done(home, run_id, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with App(home) as app:
            rec = app.runs.get(run_id)
        if not rec.running:
            return rec
        time.sleep(0.1)
    raise AssertionError("запуск не закончился")


def test_run_creates_run_with_typed_values(c, secrets_, home, kr):
    r = post(c, secrets_, "/scripts/all-types/run", {**VALID, "token": "tok-SECRET"})
    assert r.status_code == 204, r.text
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    rec = wait_done(home, run_id)
    assert rec.params == {
        "query": "ноутбук",
        "notes": "строка 1\nстрока 2",
        "max_pages": 3,
        "min_rating": 4.5,
        "in_stock": True,
        "sort": "price",
        "start_url": "https://www.ozon.ru/",
        "categories": ["https://a.ru/1", "https://a.ru/2"],
        "brands": ["Apple", "Asus"],
    }
    assert kr.store[("dawg-replay", "secret:all-types:token")] == "tok-SECRET"
    assert "tok-SECRET" not in json.dumps(rec.to_dict())
    html = c.get("/scripts/all-types").text
    assert f'href="/runs/{run_id}"' in html and "Секрет задан" in html and "tok-SECRET" not in html


def test_unchecked_bool_is_false(c, secrets_, home):
    data = {k: v for k, v in VALID.items() if k != "in_stock"}
    r = post(c, secrets_, "/scripts/all-types/run", data)
    rec = wait_done(home, r.headers["hx-redirect"].rsplit("/", 1)[1])
    assert rec.params["in_stock"] is False


def test_invalid_form_shows_errors_and_creates_no_run(c, secrets_, home):
    bad = {**VALID, "query": "   ", "max_pages": "500", "min_rating": "abc", "start_url": "ozon.ru",
           "categories": "https://a.ru/1\nhttps://a.ru/2\nhttps://a.ru/3"}
    r = post(c, secrets_, "/scripts/all-types/run", bad)
    assert r.status_code == 200
    html = r.text
    assert "Проверьте отмеченные поля: запуск не начат." in html
    assert "обязательное поле" in html and "не больше 50" in html and "нужно число" in html
    assert "нужен адрес" in html and "не больше 2 строк" in html
    assert html.count('aria-invalid="true"') == 5
    assert 'value="500"' in html and ">https://a.ru/1\nhttps://a.ru/2\nhttps://a.ru/3</textarea>" in html  # введённое сохранено
    assert runs_count(home) == 0


def test_parallel_limit_explained(c, secrets_, home):
    with App(home) as app:
        app.set_config("max_parallel_runs", "1")
        app.runs.reserve(run_id="busy", script_name="other", script_version=1, script_path="/x", draft=False,
                         preset=None, params={}, timeout_seconds=60, limit=10)
        app.db.execute("UPDATE runs SET supervisor_pid = ?, heartbeat_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = 'busy'",
                       (__import__("os").getpid(),))
    r = post(c, secrets_, "/scripts/all-types/run", VALID)
    assert r.status_code == 200
    assert "уже идут 1 запуск" in r.text and "busy" in r.text
    assert 'value="ноутбук"' in r.text
    assert runs_count(home) == 0


# --- пресеты ---


def test_save_preset_and_select(c, secrets_, home):
    r = post(c, secrets_, "/scripts/all-types/presets", {**VALID, "token": "zzz", "_preset_name": "ноутбуки"})
    assert "Пресет «ноутбуки» сохранён." in r.text
    assert '<option value="ноутбуки" selected>ноутбуки</option>' in r.text
    with App(home) as app:
        p = app.get_preset("all-types", "ноутбуки")
    assert p.params["max_pages"] == 3 and p.params["brands"] == ["Apple", "Asus"] and "token" not in p.params

    html = c.get("/scripts/all-types?preset=ноутбуки").text
    assert 'id="p-query" name="query" type="text" value="ноутбук"' in html
    frag = c.get("/scripts/all-types/_form", params={"_preset": "ноутбуки"}).text
    assert 'value="ноутбук"' in frag and ">Apple\nAsus</textarea>" in frag
    assert 'value="5"' in c.get("/scripts/all-types/_form", params={"_preset": ""}).text


def test_save_preset_requires_name_and_valid_values(c, secrets_, home):
    r = post(c, secrets_, "/scripts/all-types/presets", {**VALID, "_preset_name": " "})
    assert "Введите имя пресета." in r.text
    r = post(c, secrets_, "/scripts/all-types/presets", {**VALID, "max_pages": "0", "_preset_name": "плохой"})
    assert "не меньше 1" in r.text and "сохранён" not in r.text
    # обязательность для пресета не требуется
    r = post(c, secrets_, "/scripts/all-types/presets", {"max_pages": "7", "_preset_name": "частичный"})
    assert "Пресет «частичный» сохранён." in r.text
    with App(home) as app:
        assert [p.name for p in app.list_presets("all-types")] == ["частичный"]


def test_preset_dropped_keys_shown(c, secrets_, home, script):
    post(c, secrets_, "/scripts/all-types/presets", {**VALID, "_preset_name": "старый"})
    script(version=2, notes=False)
    html = c.get("/scripts/all-types?preset=старый").text
    assert "Текущая версия не принимает значение" in html and "«notes»: такого параметра в текущей версии скрипта нет" in html
    assert 'value="ноутбук"' in html


def test_unknown_preset(c):
    html = c.get("/scripts/all-types/_form", params={"_preset": "нет-такого"}).text
    assert "нет пресета" in html


# --- версии ---


def test_rollback(c, secrets_, home, script):
    script(version=2, notes=False)
    html = c.get("/scripts/all-types").text
    assert "all-types, версия 2" in html and 'name="notes"' not in html
    r = post(c, secrets_, "/scripts/all-types/rollback", {"_version": "1"})
    assert r.status_code == 204 and r.headers["hx-redirect"] == "/scripts/all-types"
    html = c.get("/scripts/all-types").text
    assert "all-types, версия 1" in html and 'name="notes"' in html
    r = post(c, secrets_, "/scripts/all-types/rollback", {"_version": "9"})
    assert "нет версии 9" in r.text
    r = post(c, secrets_, "/scripts/all-types/rollback", {"_version": "x"})
    assert "неверный номер версии" in r.text


def test_mutations_need_csrf(c):
    for url in ("/scripts/all-types/run", "/scripts/all-types/presets", "/scripts/all-types/rollback"):
        assert c.post(url, data={}, headers={"Origin": BASE}).status_code == 403, url


def test_history_fragment(c, secrets_, home):
    r = post(c, secrets_, "/scripts/all-types/run", VALID)
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    wait_done(home, run_id)
    frag = c.get("/scripts/all-types/_runs").text
    assert f'href="/runs/{run_id}"' in frag and "готово" in frag


def test_footer_shows_versions(c):
    import dawg_replay
    import dawg_replay_kit

    html = c.get("/").text
    assert f"dawg-replay {dawg_replay.__version__}" in html
    assert f"kit {dawg_replay_kit.__version__}" in html and "контракт 1." in html
