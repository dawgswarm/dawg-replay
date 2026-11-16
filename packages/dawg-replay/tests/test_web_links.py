"""Экран «Связи», блок связей на странице скрипта, провенанс запуска и значение «из результата» в форме."""

import json
import re
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch, links
from dawg_replay.app import App
from dawg_replay.web import links_data
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54371
BASE = f"http://127.0.0.1:{PORT}"

SOURCE = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "{name}"
# title = "{title}"
# version = 1
#
# [[outputs]]
# name = "prices.csv"
# title = "Цены"
# columns = ["sku", "price"]
# main = true
# ///
import dawg_replay_kit as kit

(kit.output_dir / "prices.csv").write_text("sku,price\\n1,10\\n2,20\\n", encoding="utf-8")
kit.result("ok", "готово", main="prices.csv")
'''

TARGET = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "update-prices"
# title = "Обновить мои цены"
# version = 1
#
# [[params]]
# name = "items"
# type = "table"
# title = "Товары"
# columns = ["sku"]
# required = true
#
# [[params]]
# name = "note"
# type = "string"
# title = "Заметка"
# ///
import dawg_replay_kit as kit

rows = kit.params["items"]
kit.result("ok", f"строк {len(rows)}")
'''


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def scripts(home, kr, tmp_path, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    src = tmp_path / "src"
    src.mkdir()
    for fname, text in (("wb.py", SOURCE.format(name="wb-prices", title="Цены WB")),
                        ("ozon.py", SOURCE.format(name="ozon-prices", title="Цены Ozon")),
                        ("upd.py", TARGET)):
        (src / fname).write_text(text, encoding="utf-8")
    with App(home, keyring_backend=kr) as a:
        for fname in ("wb.py", "ozon.py", "upd.py"):
            a.registry.add(src / fname)
    return src


@pytest.fixture
def c(home, kr, secrets_, scripts):
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        assert client.get(f"/?token={secrets_.token}").status_code == 303
        yield client


def post(c, secrets_, url, data=None, **kw):
    return c.post(url, data=data or {}, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf}, **kw)


def run_sync(home, kr, name, **kw):
    with App(home, keyring_backend=kr) as a:
        prepared = launch.prepare(a, name, **kw)
        launch.supervise(a, prepared.run.id, prepared.payload())
        return a.runs.get(prepared.run.id)


def test_empty_map_and_nav(c):
    page = c.get("/links")
    assert page.status_code == 200
    assert 'href="/links" aria-current="page"' in page.text
    assert "Связей пока нет" in page.text and "Показать все скрипты" in page.text
    page = c.get("/links?all=1").text
    assert page.count('data-node="') == 3 and "Только скрипты со связями" in page


def test_create_edit_toggle_delete_link(c, secrets_, home, kr):
    r = post(c, secrets_, "/links", {"source": "wb-prices", "target": "update-prices", "after": "ok"})
    assert r.status_code == 200 and "Связь создана" in r.text
    assert r.headers.get("HX-Trigger") == "links-changed"
    with App(home, keyring_backend=kr) as a:
        link = a.links.find("wb-prices", "update-prices")
    assert link is not None and link.starts and not link.files
    # подсказки файлов: объявленные выходы и главный файл
    assert '<option value="prices.csv">Цены: sku, price</option>' in r.text and 'value="@main"' in r.text
    assert "Товары (таблица)" in r.text

    r = post(c, secrets_, f"/links/{link.id}", {"after": "partial", "file_0": "prices.csv", "param_0": "items",
                                                "file_1": "", "param_1": "", "preset": "", "limit_per_day": "3",
                                                "enabled": "on"})
    assert "Сохранено." in r.text
    with App(home, keyring_backend=kr) as a:
        link = a.links.get(link.id)
    assert (link.cond, link.starts, link.limit_per_day) == ("partial", True, 3)
    assert link.files == (links.Transfer("prices.csv", "items"),)

    # файл без параметра: ошибка, введённое не теряется
    r = post(c, secrets_, f"/links/{link.id}", {"after": "ok", "file_0": "other.csv", "param_0": "", "enabled": "on"})
    assert "notice-error" in r.text and 'value="other.csv"' in r.text
    # нет такого параметра: ошибка проверки
    r = post(c, secrets_, f"/links/{link.id}", {"after": "ok", "file_0": "a.csv", "param_0": "nope", "enabled": "on"})
    assert "Связь не сохранена" in r.text and "nope" in r.text

    page = c.get("/links").text
    assert page.count('data-node="') == 2 and f'data-label-for="{link.id}"' in page
    assert "prices.csv → items" in page

    r = post(c, secrets_, f"/links/{link.id}/toggle")
    assert "Связь выключена." in r.text
    assert "выключена: prices.csv → items" in c.get("/links/_map").text
    r = post(c, secrets_, f"/links/{link.id}/delete")
    assert "удалена" in r.text and r.headers.get("HX-Trigger") == "links-changed"
    assert "Связей пока нет" in c.get("/links/_map").text
    assert "нет" in c.get(f"/links/{link.id}").text


def test_only_file_link_is_dashed_and_preset_ref(c, secrets_, home, kr):
    r = post(c, secrets_, "/links", {"source": "wb-prices", "target": "update-prices", "after": "never"})
    assert "Связь создана" in r.text
    with App(home, keyring_backend=kr) as a:
        link = a.links.find("wb-prices", "update-prices")
        assert not link.starts and link.files == (links.Transfer(links.MAIN, "items"),)
        a.save_preset("update-prices", "ozon", {"items": "last:ozon-prices:prices.csv"})
    page = c.get("/links").text
    assert re.search(r'class="lmap-edge is-dashed"[^>]*data-from="wb-prices"', page)
    assert "пресет «ozon»: prices.csv → items" in page
    assert 'href="/scripts/update-prices?preset=ozon#launch"' in page


def test_cycle_without_limit_is_refused(c, secrets_, home, kr):
    post(c, secrets_, "/links", {"source": "wb-prices", "target": "ozon-prices"})
    r = post(c, secrets_, "/links", {"source": "ozon-prices", "target": "wb-prices"})
    assert "notice-error" in r.text and "круг" in r.text.lower()
    r = post(c, secrets_, "/links", {"source": "wb-prices", "target": "wb-prices"})
    assert "сам себя" in r.text


def test_target_panel_mode(c, secrets_, home, kr):
    post(c, secrets_, "/links", {"source": "wb-prices", "target": "update-prices"})
    post(c, secrets_, "/links", {"source": "ozon-prices", "target": "update-prices"})
    r = c.get("/links/target/update-prices")
    assert "Если источников с запуском несколько" in r.text and "Цены WB" in r.text and "Цены Ozon" in r.text
    r = post(c, secrets_, "/links/target/update-prices", {"mode": "any", "wait_hours": "2.5", "on_timeout": "run"})
    assert "Сохранено." in r.text
    with App(home, keyring_backend=kr) as a:
        m = a.links.mode("update-prices")
    assert (m.mode, m.wait_seconds, m.on_timeout) == ("any", 9000, "run")
    assert "после любого" in c.get("/links/_map").text
    r = post(c, secrets_, "/links/target/update-prices", {"mode": "all", "wait_hours": "0", "on_timeout": "skip"})
    assert "notice-error" in r.text
    assert "не зарегистрирован" in c.get("/links/target/nope").text


def test_layout_saved(c, secrets_, home, kr):
    post(c, secrets_, "/links", {"source": "wb-prices", "target": "update-prices"})
    r = c.post("/links/_layout", content=json.dumps({"positions": {"wb-prices": [500, 260], "ghost": [1, 1]}}),
               headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf, "Content-Type": "application/json"})
    assert r.status_code == 204
    with App(home, keyring_backend=kr) as a:
        assert links_data.load_layout(a) == {"wb-prices": (500, 260)}
    assert 'data-node="wb-prices" data-x="500" data-y="260"' in c.get("/links").text
    r = c.post("/links/_layout", content=b"not json",
               headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf, "Content-Type": "application/json"})
    assert r.status_code == 400
    # без CSRF не принимается
    assert c.post("/links/_layout", content=b"{}", headers={"Origin": BASE}).status_code == 403


def test_auto_layout_columns():
    pos = links_data._auto_layout(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "a"), ("a", "c")])
    assert pos["a"][0] < pos["b"][0] < pos["c"][0]
    assert pos["d"][0] == pos["a"][0]


def test_script_block(c, secrets_, home, kr):
    page = c.get("/scripts/update-prices").text
    assert 'hx-get="/scripts/update-prices/_links"' in page and 'href="/links?script=update-prices"' in page
    frag = c.get("/scripts/update-prices/_links").text
    assert "Связей нет" in frag and "+ источник" in frag and "+ запускать после" in frag
    r = post(c, secrets_, "/links", {"source": "wb-prices", "target": "update-prices", "side": "in", "back": "script"})
    assert r.status_code == 204 and r.headers["HX-Redirect"].startswith("/links?link=")
    frag = c.get("/scripts/update-prices/_links").text
    assert "Цены WB" in frag and "только запуск" in frag
    frag = c.get("/scripts/wb-prices/_links").text
    assert "Обновить мои цены" in frag and "после успешного" in frag
    # ошибка при добавлении видна в блоке
    r = post(c, secrets_, "/links", {"source": "update-prices", "target": "wb-prices", "side": "out", "back": "script"})
    assert "notice-error" in r.text and 'id="script-links"' in r.text
    page = c.get("/links?script=update-prices").text
    assert "Если источников с запуском несколько" in page
