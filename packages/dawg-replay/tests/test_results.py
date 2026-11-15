"""Результаты запуска (контракт 1.3): result.count, links, main; превью картинок и PDF, zip,
отчёт по строкам в xlsx, история запусков в CSV, лимиты превью и журнала в настройках."""

import io
import json
import os
import sys
import zipfile

import pytest
from starlette.testclient import TestClient

from dawg_replay import repair
from dawg_replay.app import App
from dawg_replay.config import load_config
from dawg_replay.errors import ReplayError
from dawg_replay.runview import RunView
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54361
BASE = f"http://127.0.0.1:{PORT}"
RUN_ID = "20261115-150000-aaaaaa"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")


def ev(t, **f):
    return {"t": t, "ts": "2026-11-15T15:00:00.000Z", **f}


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def client(home, kr, secrets_):
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c


@pytest.fixture
def make_run(home):
    def make(events=(), files=None, finish=None, run_id=RUN_ID, script="ozon-cards"):
        with App(home) as app:
            app.runs.reserve(run_id=run_id, script_name=script, script_version=None, script_path="/x/script.py",
                             draft=True, preset=None, params={}, timeout_seconds=60, limit=100)
            run_dir = app.paths.run_dir(run_id)
            out = run_dir / "output"
            out.mkdir(parents=True, exist_ok=True)
            for name, data in (files or {}).items():
                p = out / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            with open(run_dir / "events.jsonl", "w", encoding="utf-8", newline="") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            if finish:
                app.runs.finish(run_id, **finish)
        return run_dir

    return make


# --- kit -------------------------------------------------------------------------


class ListSink:
    def __init__(self):
        self.events = []

    def write(self, e):
        self.events.append(e)

    def close(self):
        pass


def test_kit_result_extras_and_checks(tmp_path):
    from dawg_replay_kit.events import ConsoleSink, Emitter

    (tmp_path / "cards.xlsx").write_bytes(b"x")
    sink = ListSink()
    em = Emitter(sink, output_dir=tmp_path, auto_flush=False)
    em.result("ok", "готово", count=120, main="cards.xlsx",
              links=[{"title": "Таблица", "url": " https://docs.example.com/x "}, {"url": "http://a.ru"}])
    e = sink.events[-1]
    assert (e["count"], e["main"]) == (120, "cards.xlsx")
    assert e["links"] == [{"title": "Таблица", "url": "https://docs.example.com/x"}, {"title": "http://a.ru", "url": "http://a.ru"}]
    em.result("ok", "без полей")
    assert not {"count", "links", "main"} & set(sink.events[-1])
    for kw in ({"count": -1}, {"count": True}, {"count": 1.5}, {"links": [{"url": "javascript:alert(1)"}]},
               {"links": [{"title": "x"}]}, {"links": ["https://a.ru"]}):
        with pytest.raises(ValueError):
            em.result("ok", "x", **kw)
    with pytest.raises(ValueError):
        em.result("ok", "x", main="../outside.txt")
    with pytest.raises(FileNotFoundError):
        em.result("ok", "x", main="нет.xlsx")
    line = ConsoleSink(io.StringIO(), tmp_path).format(e)
    assert "ИТОГ ok: готово (записей: 120)" in line and "cards.xlsx" in line and "ссылка «Таблица»" in line


# --- приложение: разбор итога и тихая поломка -------------------------------------


def test_runview_result_extras_filtered(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "output").mkdir(parents=True)
    with open(run_dir / "events.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(ev("result", status="ok", summary="s", count=5, main="../../etc/passwd",
                              links=[{"title": "ok", "url": "https://a.ru"}, {"title": "js", "url": "javascript:x"},
                                     "мусор"])) + "\n")
    r = RunView(run_dir).snapshot()["result"]
    assert r["count"] == 5 and r["main"] is None and r["links"] == [{"title": "ok", "url": "https://a.ru"}]


def test_count_goes_first_in_quiet_break(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "output").mkdir(parents=True)
    (run_dir / "output" / "cards.csv").write_text("sku;price\n", encoding="utf-8")  # одна шапка
    with open(run_dir / "events.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(ev("rows", table="t", rows=[{"a": 1}] * 30)) + "\n")
        f.write(json.dumps(ev("artifact", path="cards.csv", label="CSV")) + "\n")
        f.write(json.dumps(ev("result", status="ok", summary="s", count=0)) + "\n")
    s = repair.scan(run_dir)
    assert s.counter() == ("count", 0)
    sus = repair.judge(s.counter(), has_files=True, history=[])
    assert sus["kind"] == "empty" and "0 записей" in sus["text"]
    drop = repair.judge(("count", 3), has_files=True, history=[100, 110, 90])
    assert drop["kind"] == "drop" and "3 записи" in drop["text"]


# --- экран запуска -----------------------------------------------------------------


def test_previews_main_links_zip_on_run_screen(client, make_run):
    pdf = b"%PDF-1.4\n%%EOF\n"
    make_run([
        ev("artifact", path="shots/sent.png", label="Скриншот: заявка", mime="image/png"),
        ev("artifact", path="report.pdf", label="Отчёт", mime="application/pdf"),
        ev("artifact", path="logo.svg", label="SVG", mime="image/svg+xml"),
        ev("artifact", path="page.html", label="HTML", mime="text/html"),
        ev("artifact", path="cards.xlsx", label="Карточки"),
        ev("result", status="ok", summary="готово", count=2, main="cards.xlsx",
           links=[{"title": "Таблица клиента", "url": "https://docs.example.com/t"}, {"title": "x", "url": "javascript:alert(1)"}]),
    ], files={"shots/sent.png": PNG, "report.pdf": pdf, "logo.svg": b"<svg onload=alert(1)/>",
              "page.html": b"<script>alert(1)</script>", "cards.xlsx": b"xlsx"},
       finish={"status": "ok", "reason": None, "summary": "готово"})
    h = client.get(f"/runs/{RUN_ID}").text
    assert f'<img src="/runs/{RUN_ID}/view/shots/sent.png"' in h and f'<iframe src="/runs/{RUN_ID}/view/report.pdf"' in h
    assert "view/logo.svg" not in h and "view/page.html" not in h
    assert h.index("Открыть результат") < h.index("Скачать report.pdf") and f'/runs/{RUN_ID}/files/cards.xlsx' in h
    assert 'href="https://docs.example.com/t" rel="noopener noreferrer" target="_blank">Таблица клиента' in h
    assert "javascript:alert" not in h
    assert f"/runs/{RUN_ID}/output.zip" in h

    r = client.get(f"/runs/{RUN_ID}/view/shots/sent.png")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline") and r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "SAMEORIGIN"
    r = client.get(f"/runs/{RUN_ID}/view/report.pdf")
    assert r.status_code == 200 and "frame-ancestors 'self'" in r.headers["content-security-policy"]
    for bad in ("logo.svg", "page.html", "cards.xlsx", "../events.jsonl", "nope.png"):
        assert client.get(f"/runs/{RUN_ID}/view/{bad}").status_code == 404, bad
    page = client.get(f"/runs/{RUN_ID}")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"] and page.headers["x-frame-options"] == "DENY"


def test_zip_streams_output_without_links_outside(client, make_run, tmp_path):
    run_dir = make_run([ev("artifact", path="a.csv", label="A")], files={"a.csv": b"1;2\n", "sub/b.txt": b"b"},
                       finish={"status": "ok", "reason": None})
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    if sys.platform != "win32":
        os.symlink(secret, run_dir / "output" / "link.txt")
    r = client.get(f"/runs/{RUN_ID}/output.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert f'{RUN_ID}.zip' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["a.csv", "sub/b.txt"]
    assert z.read("sub/b.txt") == b"b" and b"SECRET" not in r.content


def test_items_xlsx(client, make_run):
    from openpyxl import load_workbook

    make_run([ev("item", key="a@b.ru", status="ok", row=2), ev("item", key="c@d.ru", status="failed", msg="нет", row=3)],
             finish={"status": "partial", "reason": None})
    assert f"/runs/{RUN_ID}/items.xlsx" in client.get(f"/runs/{RUN_ID}").text
    r = client.get(f"/runs/{RUN_ID}/items.xlsx")
    assert r.status_code == 200
    ws = load_workbook(io.BytesIO(r.content)).active
    assert [list(row) for row in ws.iter_rows(values_only=True)] == [
        ["Строка", "Номер в таблице", "Статус", "Сообщение"], ["a@b.ru", 2, "сделана", None], ["c@d.ru", 3, "упала", "нет"]]


def test_script_history_csv(client, make_run, home, write_script):
    with App(home) as app:
        app.registry.add(write_script(version=1))
    make_run([ev("result", status="ok", summary="собрано 5", count=5)], run_id="20261115-150000-bbbbbb",
             finish={"status": "ok", "reason": None, "summary": "собрано 5"})
    make_run([ev("item", key=1, status="ok", row=2)], run_id="20261115-150100-cccccc",
             finish={"status": "failed", "reason": "exit_code", "exit_code": 1, "error": "упал"})
    assert "/scripts/ozon-cards/runs.csv" in client.get("/scripts/ozon-cards").text
    r = client.get("/scripts/ozon-cards/runs.csv")
    assert r.status_code == 200 and r.text.startswith("﻿Запуск;Статус;Начат;Закончен")
    lines = r.text.strip().splitlines()
    assert lines[1].startswith("20261115-150100-cccccc;ошибка;") and lines[1].endswith(";1;1;;упал")
    assert lines[2].startswith("20261115-150000-bbbbbb;готово;") and ";5;;;собрано 5;" in lines[2]
    assert client.get("/scripts/nope/runs.csv").status_code == 404


# --- лимиты ------------------------------------------------------------------------


def test_limits_bounds_and_settings(client, secrets_, home):
    with App(home) as app:
        for key, value in (("log_limit", "50"), ("log_limit", "30000"), ("rows_preview_limit", "5")):
            with pytest.raises(ReplayError) as e:
                app.set_config(key, value)
            assert "от " in e.value.message
        app.set_config("log_limit", "300")
        assert app.config.log_limit == 300
    (home / "config.toml").write_text("rows_preview_limit = 3\nlog_limit = 999999\n", encoding="utf-8")
    cfg = load_config(home / "config.toml")  # старый файл с прежними границами не ломает запуск
    assert (cfg.rows_preview_limit, cfg.log_limit) == (10, 20000)
    form = {"max_parallel_runs": "3", "max_timeout_minutes": "60", "rows_preview_limit": "200", "log_limit": "50"}
    r = client.post("/settings/limits", data=form, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf})
    assert "от 100 до 20000" in r.text
    r = client.post("/settings/limits", data={**form, "log_limit": "500"}, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf})
    assert "Ограничения сохранены" in r.text
    with App(home) as app:
        assert (app.config.rows_preview_limit, app.config.log_limit) == (200, 500)
    assert 'id="log_limit"' in client.get("/settings").text


def test_main_button_icon_by_kind_and_report_buttons_together(client, make_run):
    # dr-91: «Открыть результат» со значком новой вкладки, если файл открывается для просмотра; иначе скачать.
    make_run([ev("artifact", path="shot.png", label="Скриншот", mime="image/png"),
              ev("item", key="a", status="ok", row=2),
              ev("result", status="ok", summary="готово", main="shot.png")],
             files={"shot.png": PNG}, finish={"status": "ok", "reason": None, "summary": "готово"})
    h = client.get(f"/runs/{RUN_ID}").text
    main = h[h.index('aria-label="Главный файл результата"'):]
    main = main[:main.index("</a>")]
    assert 'target="_blank"' in main and 'title="Откроется в новой вкладке"' in main and "M11 4h5v5" in main
    tools = h[h.index('class="items-download"'):]
    tools = tools[:tools.index("</span>\n  </div>")]
    assert "Скачать отчёт:" in tools and "items.csv" in tools and "items.xlsx" in tools


def test_main_button_downloads_non_preview(client, make_run):
    make_run([ev("artifact", path="cards.csv", label="CSV"), ev("result", status="ok", summary="готово", main="cards.csv")],
             files={"cards.csv": b"a\n"}, finish={"status": "ok", "reason": None, "summary": "готово"}, run_id=RUN_ID)
    h = client.get(f"/runs/{RUN_ID}").text
    main = h[h.index('aria-label="Главный файл результата"'):]
    main = main[:main.index("</a>")]
    assert " download" in main and 'title="Скачать"' in main and "M10 3.5v9" in main
