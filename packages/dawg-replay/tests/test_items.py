"""Отчёт по строкам (item): экран запуска, CSV, списки, тихая поломка."""

import json
import re

import pytest
from starlette.testclient import TestClient

from dawg_replay import repair
from dawg_replay.app import App
from dawg_replay.runview import RunView
from dawg_replay.web.run_screen import items_csv, items_shown
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app
from dawg_replay.web.views import items_text

PORT = 54333
BASE = f"http://127.0.0.1:{PORT}"


def item(key, status, msg=None, row=None):
    e = {"t": "item", "ts": "2026-11-15T12:00:00.000Z", "key": key, "status": status}
    if msg is not None:
        e["msg"] = msg
    if row is not None:
        e["row"] = row
    return e


def write(run_dir, events):
    (run_dir / "output").mkdir(parents=True, exist_ok=True)
    with open(run_dir / "events.jsonl", "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# --- RunView ---


def test_runview_items_replace_by_key_and_count(tmp_path):
    d = tmp_path / "r1"
    write(d, [item("a", "failed", "нет кнопки", 2), item("b", "ok", row=3), item(1, "skipped")])
    v = RunView(d)
    snap = v.snapshot()
    assert snap["items"]["counts"] == {"ok": 1, "failed": 1, "skipped": 1, "total": 3}
    seq = snap["seq"]
    write(d, [item("a", "ok", row=2), item("1", "failed", "повтор")])  # 1 и "1" одна строка
    ch = v.changes(seq)["changes"]
    assert [c["kind"] for c in ch] == ["item", "item"]
    assert ch[-1]["data"]["counts"] == {"ok": 2, "failed": 1, "skipped": 0, "total": 3}
    rows = v.snapshot()["items"]["rows"]
    assert [(r["key"], r["status"]) for r in rows] == [("a", "ok"), ("b", "ok"), ("1", "failed")]
    assert rows[0]["row"] == 2


def test_runview_bad_item_goes_to_log(tmp_path):
    d = tmp_path / "r1"
    write(d, [item("a", "done"), {"t": "item", "key": [1], "status": "ok"}])
    snap = RunView(d).snapshot()
    assert snap["items"] is None
    assert [e["level"] for e in snap["log"]] == ["raw", "raw"]


def test_runview_no_items(tmp_path):
    d = tmp_path / "r1"
    write(d, [{"t": "log", "level": "info", "msg": "x"}])
    assert RunView(d).snapshot()["items"] is None


# --- текст и показ ---


def test_items_text():
    assert items_text(None) == "" and items_text({"total": 0}) == ""
    assert items_text({"ok": 48, "failed": 2, "skipped": 0, "total": 50}) == "48 из 50, 2 упали"
    assert items_text({"ok": 45, "failed": 1, "skipped": 4, "total": 50}) == "45 из 50, 1 упала, 4 пропущены"
    assert items_text({"ok": 5000, "failed": 0, "skipped": 0, "total": 5000}) == "5\u202f000 из 5\u202f000"


def test_items_shown_keeps_failed_and_tail():
    rows = [{"key": i, "status": "failed" if i in (3, 500) else "ok"} for i in range(1000)]
    shown = items_shown(rows, limit=10)
    keys = [r["key"] for r in shown]
    assert len(shown) == 10 and 3 in keys and 500 in keys
    assert keys == sorted(keys) and keys[-1] == 999
    assert items_shown(rows[:5], limit=10) == rows[:5]


def test_items_csv_excel_and_formulas():
    text = items_csv([{"key": "=HYPERLINK(1)", "status": "failed", "msg": "-x", "row": 2},
                      {"key": 5, "status": "ok", "msg": None, "row": None}])
    assert text.startswith("﻿Строка;Номер в таблице;Статус;Сообщение\r\n")
    assert "'=HYPERLINK(1);2;упала;'-x\r\n" in text and "5;;сделана;\r\n" in text


# --- тихая поломка и списки ---


@pytest.fixture
def make_run(home, kr, write_script):
    with App(home, keyring_backend=kr) as a:
        a.registry.add(write_script(version=1))
    counter = iter(range(10, 99))

    def make(events=(), status="ok", files=()):
        rid = f"20261115-1200{next(counter):02d}-bbbbbb"
        with App(home, keyring_backend=kr) as a:
            a.runs.reserve(run_id=rid, script_name="ozon-cards", script_version=1, script_path="/x/v1/script.py",
                           draft=False, preset=None, params={"query": "q"}, timeout_seconds=60, limit=100)
            d = a.paths.run_dir(rid)
            write(d, events)
            for f in files:
                (d / "output" / f).write_text("x", encoding="utf-8")
            a.runs.finish(rid, status=status, reason=None, exit_code=0, summary=None)
        return rid

    return make


def _done(n, failed=0, skipped=0):
    return ([item(f"k{i}", "ok") for i in range(n)] + [item(f"f{i}", "failed", "ошибка") for i in range(failed)]
            + [item(f"s{i}", "skipped") for i in range(skipped)])


def test_counter_prefers_items_and_counts_skipped(make_run, home, kr):
    rid = make_run(_done(3, failed=2, skipped=4) + [{"t": "rows", "table": "t", "rows": [{"a": 1}] * 50}])
    with App(home, keyring_backend=kr) as a:
        s = repair.scan(a.paths.run_dir(rid))
        assert s.items == {"ok": 3, "failed": 2, "skipped": 4}
        assert s.counter() == ("items", 7)
        # из кэша на диске то же
        repair._cache.clear()
        assert repair.scan(a.paths.run_dir(rid)).items == {"ok": 3, "failed": 2, "skipped": 4}
        cache = json.loads((a.paths.run_dir(rid) / repair.CACHE_FILE).read_text("utf-8"))
        assert cache["v"] == repair.CACHE_VERSION == 2


def test_silent_breakage_by_items(make_run, home, kr):
    for _ in range(3):
        make_run(_done(50))
    rerun = make_run(_done(0, skipped=50))  # повторный запуск: всё уже сделано, не поломка
    broken = make_run(_done(5, failed=0))
    nothing = make_run(_done(0, failed=3))
    with App(home, keyring_backend=kr) as a:
        assert repair.suspicion(a, a.runs.get(rerun)) is None
        s = repair.suspicion(a, a.runs.get(broken))
        assert s["kind"] == "drop" and s["text"].startswith("Сделано 5 строк, а в последних")
        e = repair.suspicion(a, a.runs.get(nothing))
        assert e["kind"] == "empty" and "ни одна строка не сделана" in e["text"]


def test_annotate_items_in_lists(make_run, home, kr):
    rid = make_run(_done(48, failed=2), status="partial")
    plain = make_run([{"t": "rows", "table": "t", "rows": [{"a": 1}]}])
    with App(home, keyring_backend=kr) as a:
        runs = repair.annotate(a, [a.runs.get(rid).to_dict(), a.runs.get(plain).to_dict()])
    assert runs[0]["items"] == {"ok": 48, "failed": 2, "skipped": 0, "total": 50}
    assert runs[1]["items"] is None


# --- веб ---


@pytest.fixture
def client(home, kr):
    s = Secrets(port=PORT)
    with TestClient(create_app(home, s, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={s.token}").status_code == 303
        yield c


def test_run_screen_items_panel_and_csv(client, make_run):
    rid = make_run([item("a@x.ru", "ok", row=2), item("b@x.ru", "failed", "<b>нет</b> кнопки", 3),
                    item("c@x.ru", "skipped", "уже сделано", 4)], status="partial")
    r = client.get(f"/runs/{rid}")
    assert r.status_code == 200
    panel = re.search(r'<section id="run-items".*?</section>', r.text, re.S).group(0)
    assert "1 из 3 сделана" in panel and "1 упала" in panel and "1 пропущена" in panel
    assert "только упавшие" in panel and "&lt;b&gt;нет&lt;/b&gt; кнопки" in panel
    assert 'class="item-row item-failed"' in panel and "№&nbsp;3" in panel

    csv = client.get(f"/runs/{rid}/items.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv.headers["content-disposition"]
    assert "b@x.ru;3;упала;<b>нет</b> кнопки" in csv.content.decode("utf-8-sig")


def test_run_screen_without_items_has_empty_panel(client, make_run):
    rid = make_run([{"t": "log", "level": "info", "msg": "x"}])
    r = client.get(f"/runs/{rid}")
    assert 'id="run-items" class="run-section is-empty"' in r.text and "Строки</h2>" not in r.text


def test_lists_show_items_text(client, make_run):
    make_run(_done(48, failed=2), status="partial")
    for url in ("/scripts/ozon-cards", "/scripts", "/"):
        r = client.get(url)
        assert "48 из 50, 2 упали" in r.text, url


def test_live_patch_redraws_items_panel(tmp_path):
    from starlette.templating import Jinja2Templates

    from dawg_replay.web import run_screen, server, views

    templates = Jinja2Templates(directory=str(server.HERE / "templates"))
    views.setup_templates(templates, Secrets(port=PORT))
    run_screen.setup(templates)
    d = tmp_path / "r1"
    write(d, [item("a", "ok")])
    v = RunView(d)
    seq = v.snapshot()["seq"]
    write(d, [item("b", "failed", "нет")])
    patch = run_screen.patch_for(templates, v, v.changes(seq)["changes"])
    assert list(patch["panels"]) == ["items"]
    assert "только упавшие" in patch["panels"]["items"] and 'class="item-row item-failed"' in patch["panels"]["items"]
