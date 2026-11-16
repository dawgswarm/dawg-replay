"""Значение «из результата другого скрипта» в форме и пресете, источники запуска и «Передать в другой скрипт»."""

import json
import re
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.web import transfer_data
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


def test_run_form_value_from_result(c, secrets_, home, kr):
    src = run_sync(home, kr, "wb-prices")
    assert src.status == "ok"
    form = c.get("/scripts/update-prices").text
    assert 'name="items__ref"' in form
    assert '<option value="last:wb-prices:prices.csv">Цены WB: prices.csv, последний успешный</option>' in form
    # Ozon ещё не запускался, но выходы объявлены: их тоже можно выбрать
    assert 'value="last:ozon-prices:prices.csv"' in form and 'value="last:ozon-prices:@main"' in form
    r = post(c, secrets_, "/scripts/update-prices/run", {"_preset": "", "items": "", "items__ref": "last:wb-prices:prices.csv"})
    assert r.status_code == 204, r.text
    run_id = r.headers["HX-Redirect"].rsplit("/", 1)[1]
    with App(home, keyring_backend=kr) as a:
        rec = a.runs.get(run_id)
    assert rec.inputs and rec.inputs[0]["run"] == src.id and rec.inputs[0]["param"] == "items"
    # мусор в ссылке: ошибка у поля, выбор не теряется
    r = post(c, secrets_, "/scripts/update-prices/run", {"_preset": "", "items": "", "items__ref": "garbage"})
    assert "выберите результат из списка" in r.text
    # без значения: обязательная таблица
    r = post(c, secrets_, "/scripts/update-prices/run", {"_preset": "", "items": ""})
    assert "загрузите таблицу" in r.text


def test_preset_keeps_result_ref(c, secrets_, home, kr):
    run_sync(home, kr, "wb-prices")
    r = post(c, secrets_, "/scripts/update-prices/presets",
             {"_preset_name": "из WB", "items": "", "items__ref": "last:wb-prices:prices.csv"})
    assert r.status_code == 200
    with App(home, keyring_backend=kr) as a:
        assert a.get_preset("update-prices", "из WB").results == {"items": "last:wb-prices:prices.csv"}
    form = c.get("/scripts/update-prices?preset=из WB").text
    assert re.search(r'<option value="last:wb-prices:prices.csv"\s+selected>', form)


def test_provenance_and_transfer(c, secrets_, home, kr):
    src = run_sync(home, kr, "wb-prices")
    page = c.get(f"/runs/{src.id}").text
    assert "Передать в другой скрипт" in page and "Обновить мои цены" in page
    assert "Запущен после" not in page
    frag = c.get(f"/runs/{src.id}/_transfer?script=update-prices").text
    assert 'action="/scripts/update-prices#launch"' in frag and f'name="from_run" value="{src.id}"' in frag
    assert re.search(r'<option value="prices.csv"\s+selected>', frag)  # колонки совпали с объявленными
    assert "Выберите скрипт" in c.get(f"/runs/{src.id}/_transfer?script=nope").text

    form = c.get(f"/scripts/update-prices?from_run={src.id}&use_items=prices.csv").text
    assert "подставлены в форму" in form and "Запускать так всегда" not in form
    assert re.search(rf'<option value="run:{src.id}:prices.csv"\s+selected>Цены WB, запуск от', form)
    r = post(c, secrets_, "/scripts/update-prices/run", {"_preset": "", "items": "", "items__ref": f"run:{src.id}:prices.csv"})
    assert r.status_code == 204, r.text
    got = r.headers["HX-Redirect"].rsplit("/", 1)[1]
    with App(home, keyring_backend=kr) as a:
        assert a.runs.get(got).inputs[0]["run"] == src.id
    page = c.get(f"/runs/{got}").text
    assert "<code>items</code> из результата Цены WB: prices.csv" in page
    page = c.get(f"/runs/{src.id}").text
    assert "Передал в:" in page and f'href="/runs/{got}"' in page and "взял файл" in page


def test_provenance_after_source_run(home, kr, scripts):
    src = run_sync(home, kr, "wb-prices")
    with App(home, keyring_backend=kr) as a:
        prepared = launch.prepare(a, "update-prices", sets=["items=last:wb-prices:prices.csv"], source_run=src.id)
        rec = a.runs.get(prepared.run.id)
        prov = transfer_data.provenance(a, rec)
        assert prov["after"]["script"] == "wb-prices" and prov["after"]["run_id"] == src.id
        assert prov["inputs"][0]["run_id"] == src.id
        back = transfer_data.provenance(a, a.runs.get(src.id))
        assert [p["id"] for p in back["passed"]] == [rec.id] and back["passed"][0]["started"]
        a.runs.finish(rec.id, status="ok", reason=None)


def test_links_screen_is_gone(c):
    assert c.get("/links").status_code == 404
    assert 'href="/links"' not in c.get("/").text
    assert "/_links" not in c.get("/scripts/update-prices").text
