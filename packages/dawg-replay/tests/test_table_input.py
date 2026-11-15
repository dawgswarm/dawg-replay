"""Таблица по объявлению скрипта в приложении (контракт 1.3, dr-85): input, лист XLSX, типы колонок."""

import io
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dawg_replay import forms, launch, schedules
from dawg_replay.app import App
from dawg_replay.uploads import Uploads
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54338
BASE = f"http://127.0.0.1:{PORT}"


def script(name: str, extra: str) -> str:
    return f'''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.2,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "{name}"
# title = "Заказы"
# version = 1
#
# [[params]]
# name = "orders"
# type = "table"
# title = "Заказы"
# required = true
# columns = ["order"]
{extra}# ///

import json
import dawg_replay_kit as kit

(kit.output_dir / "seen.json").write_text(json.dumps(kit.params.orders, ensure_ascii=False), encoding="utf-8")
'''


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def add(app, tmp_path):
    def add(name, extra=""):
        f = tmp_path / f"{name}.py"
        f.write_text(script(name, extra), encoding="utf-8")
        app.registry.add(f)
        return name
    return add


TYPED = '# column_types = { qty = "int", price = "float", paid = "bool", day = "date" }\n'
CSV = "order;qty;price;paid;day\nA-1;2;99,5;да;15.11.2026\nA-2;;10;нет;2026-11-16\n"


def xlsx(sheets):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- загрузка, типы, листы ---


def test_upload_converts_types(app, add):
    add("typed", TYPED)
    fl = forms.upload_table(app, "typed", "orders", CSV.encode(), "заказы.csv")["field"]
    assert fl.error is None
    rows = Uploads(app.paths.uploads).get(fl.table["id"])["rows"]
    assert rows == [{"order": "A-1", "qty": 2, "price": 99.5, "paid": True, "day": "2026-11-15", "_row": 2},
                    {"order": "A-2", "qty": None, "price": 10.0, "paid": False, "day": "2026-11-16", "_row": 3}]
    bad = forms.upload_table(app, "typed", "orders", b"order;qty\nA-1;2\nA-2;\nA-3;two\n", "z.csv")["field"]
    assert bad.error == "строка 4, колонка «qty»: ожидается целое число, получено «two»"


def test_upload_tsv_and_json(app, add):
    add("plain")
    fl = forms.upload_table(app, "plain", "orders", b"order\tx\nA-1\t1\n", "a.tsv")["field"]
    assert fl.error is None and fl.table["count"] == 1
    fl = forms.upload_table(app, "plain", "orders", json.dumps([{"order": "A-1"}, {"order": "A-2"}]).encode(), "a.json")["field"]
    assert fl.error is None and fl.table["count"] == 2


def test_xlsx_sheet_choice_after_upload(app, add):
    add("plain")
    data = xlsx({"Сводка": [["order"], ["S-1"]], "Заказы": [["order"], ["A-1"], ["A-2"]]})
    fl = forms.upload_table(app, "plain", "orders", data, "книга.xlsx")["field"]
    assert fl.table["sheets"] == ["Сводка", "Заказы"] and fl.table["sheet"] == "Сводка" and fl.table["count"] == 1
    other = forms.table_sheet(app, "plain", "orders", fl.table["id"], "Заказы")["field"]
    assert other.table["sheet"] == "Заказы" and other.table["count"] == 2
    rows = Uploads(app.paths.uploads).get(other.table["id"])["rows"]
    assert [r["order"] for r in rows] == ["A-1", "A-2"]
    expired = forms.table_sheet(app, "plain", "orders", "A" * 22, "Заказы")["field"]
    assert "устарела" in expired.error


def test_declared_sheet_is_used_without_choice(app, add):
    add("sheet", '# sheet = "Заказы"\n')
    data = xlsx({"Сводка": [["order"], ["S-1"]], "Заказы": [["order"], ["A-1"], ["A-2"]]})
    fl = forms.upload_table(app, "sheet", "orders", data, "книга.xlsx")["field"]
    assert fl.table["count"] == 2 and "sheets" not in fl.table


# --- способы ввода ---


@pytest.fixture
def web(home, kr, add):
    add("fileonly", '# input = ["file", "preset"]\n')
    add("manualonly", '# input = ["manual"]\n')
    add("plain")
    s = Secrets(port=PORT)
    with TestClient(create_app(home, s, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={s.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": s.csrf}


def test_form_shows_only_declared_inputs(web):
    c, h = web
    page = c.get("/scripts/fileonly").text
    assert 'name="_table_file"' in page and "data-manual-open" not in page
    page = c.get("/scripts/manualonly").text
    assert 'name="_table_file"' not in page and "data-manual-open" in page and "файл .csv" not in page
    page = c.get("/scripts/plain").text
    assert 'name="_table_file"' in page and "data-manual-open" in page
    r = c.post("/scripts/manualonly/_table/orders", files={"_table_file": ("a.csv", b"order\nA\n", "text/csv")}, headers=h)
    assert r.status_code == 404 and "не принимает файл" in r.text
    r = c.post("/scripts/fileonly/_table/orders/manual", data={"_cols": ["order"], "_cells": ["A"]}, headers=h)
    assert r.status_code == 404 and "не принимает ввод вручную" in r.text


def test_web_sheet_switch(web):
    c, h = web
    data = xlsx({"Сводка": [["order"], ["S-1"]], "Заказы": [["order"], ["A-1"], ["A-2"]]})
    r = c.post("/scripts/plain/_table/orders", files={"_table_file": ("книга.xlsx", data, "application/octet-stream")}, headers=h)
    assert "Лист" in r.text and '<option value="Заказы"' in r.text
    uid = r.text.split('name="orders" value="', 1)[1].split('"', 1)[0]
    r = c.post("/scripts/plain/_table/orders/sheet", data={"_upload": uid, "_sheet": "Заказы"}, headers=h)
    assert r.status_code == 200 and "2 строки" in r.text and '<option value="Заказы" selected>' in r.text


def test_cli_respects_inputs(app, add, cli, tmp_path):
    add("fileonly", '# input = ["file", "preset"]\n')
    add("manualonly", '# input = ["manual"]\n')
    f = tmp_path / "o.csv"
    f.write_text("order\nA-1\n", encoding="utf-8")
    code, data = cli("run", "fileonly", "--row", "orders", "order=A-1")
    assert code == 1 and "не принимает эту таблицу строками" in data["error"]["message"]
    assert "можно файлом (имя=@файл), из пресета (--preset)" in data["error"]["message"]
    code, data = cli("run", "manualonly", "--set", f"orders=@{f}")
    assert code == 1 and "не принимает эту таблицу файлом" in data["error"]["message"]
    code, data = cli("preset", "save", "manualonly", "p", "--row", "orders", "order=A-1")
    assert code == 0 and data["preset"]["params"] == {}
    assert "в пресет не сохраняется: скрипт берёт её только при запуске" in data["note"]


def test_schedule_gap_for_table_only_at_run(app, add):
    add("manualonly", '# input = ["manual"]\n')
    app.save_preset("manualonly", "p", {})
    gap = schedules.preset_gap(app.registry.manifest("manualonly"), app.get_preset("manualonly", "p"))
    assert "только при запуске" in gap and "по расписанию" in gap


def test_old_string_preset_is_typed_on_load(app, add):
    add("typed", TYPED)
    # Пресет из прошлой версии скрипта: таблица строками.
    app.db.execute("INSERT INTO presets (script_name, name, params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                   ("typed", "old", json.dumps({"orders": [{"order": "A-1", "qty": "3", "_row": 2}]}), "x", "x"))
    p = app.get_preset("typed", "old")
    assert p.params["orders"] == [{"order": "A-1", "qty": 3, "_row": 2}] and p.dropped == []


def test_run_gets_typed_rows(app, add, cli, home, tmp_path):
    add("typed", TYPED)
    f = tmp_path / "o.csv"
    f.write_text(CSV, encoding="utf-8")
    code, data = cli("run", "typed", "--set", f"orders=@{f}", "--follow")
    assert code == 0, data
    seen = json.loads((Path(home) / "runs" / data["run"]["id"] / "output" / "seen.json").read_text("utf-8"))
    assert seen[0] == {"order": "A-1", "qty": 2, "price": 99.5, "paid": True, "day": "2026-11-15", "_row": 2}
    assert seen[1]["qty"] is None


def test_sheet_choice_survives_bad_sheet(app, add):
    add("typed", TYPED)
    data = xlsx({"Сводка": [["order", "qty"], ["S-1", "1"]], "Заказы": [["order", "qty"], ["A-1", "x"]]})
    fl = forms.upload_table(app, "typed", "orders", data, "книга.xlsx")["field"]
    bad = forms.table_sheet(app, "typed", "orders", fl.table["id"], "Заказы")["field"]
    assert bad.error == "строка 2, колонка «qty»: ожидается целое число, получено «x»"
    assert bad.table["failed"] and bad.table["sheets"] == ["Сводка", "Заказы"] and bad.value == ""
    back = forms.table_sheet(app, "typed", "orders", bad.table["id"], "Сводка")["field"]
    assert back.error is None and back.table["count"] == 1
