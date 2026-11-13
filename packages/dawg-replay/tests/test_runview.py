"""Состояние запуска из events.jsonl (dawg_replay.runview)."""

import json
import threading
import time
from pathlib import Path

import pytest

from dawg_replay.runview import RunView


def ev(t, **f):
    return {"t": t, "ts": "2026-11-14T12:00:00.000Z", **f}


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "r1"
    (d / "output").mkdir(parents=True)
    return d


def write(run_dir, *items, mode="a"):
    with open(run_dir / "events.jsonl", mode + "b") as f:
        for it in items:
            if isinstance(it, bytes):
                f.write(it)
            elif isinstance(it, str):
                f.write((it + "\n").encode("utf-8"))
            else:
                f.write((json.dumps(it, ensure_ascii=False) + "\n").encode("utf-8"))


def test_all_event_types(run_dir):
    (run_dir / "output" / "cards.csv").write_text("a\n1\n", encoding="utf-8")
    write(
        run_dir,
        ev("stage", name="Открываем"),
        ev("session", action="provisioning"),
        ev("session", action="open", session_id="s1", browser_id="b1"),
        ev("stage", name="Собираем"),
        ev("progress", id="pages", label="Страницы", done=1, total=3),
        ev("progress", id="pages", label="Страницы", done=2, total=3),
        ev("progress", id="items", label="Карточки", done=10),
        ev("metric", key="found", label="Найдено", value=120, unit="шт"),
        ev("metric", key="found", label="Найдено", value=130, unit="шт"),
        ev("rows", table="cards", columns=["id", "name"], rows=[{"id": 1, "name": "a"}]),
        ev("rows", table="cards", rows=[{"id": 2, "name": "b", "extra": 1}]),
        ev("rows", table="other", rows=[{"x": 1}, {"y": 2, "x": 3}]),
        ev("log", level="info", msg="страница 1"),
        ev("log", level="warn", msg="медленно"),
        ev("artifact", path="cards.csv", label="CSV", mime="text/csv"),
        ev("session", action="provisioning"),
        ev("session", action="failed"),
        ev("session", action="close", session_id="s1"),
        ev("result", status="partial", summary="120 из 130"),
    )
    s = RunView(run_dir).snapshot()
    assert s["stage"] == "Собираем" and [t["name"] for t in s["timeline"]] == ["Открываем", "Собираем"]
    assert s["progress"]["pages"]["done"] == 2 and s["progress"]["pages"]["total"] == 3
    assert s["progress"]["items"]["total"] is None
    assert s["metrics"]["found"]["value"] == 130 and s["metrics"]["found"]["unit"] == "шт"
    cards = s["tables"]["cards"]
    assert cards["columns"] == ["id", "name"]  # из события
    assert [r["id"] for r in cards["rows"]] == [1, 2] and cards["total"] == 2 and not cards["truncated"]
    assert s["tables"]["other"]["columns"] == ["x", "y"]  # из ключей, в порядке появления
    assert [(e["level"], e["msg"]) for e in s["log"]] == [("info", "страница 1"), ("warn", "медленно")]
    assert s["artifacts"] == [
        {"path": "cards.csv", "label": "CSV", "mime": "text/csv", "exists": True, "size": 4, "ts": "2026-11-14T12:00:00.000Z"}
    ]
    assert s["browsers"] == {"open": 0, "provisioning": 0, "sessions": []}
    assert s["result"]["status"] == "partial" and s["result"]["summary"] == "120 из 130"
    assert s["seq"] > 0 and s["position"] == (run_dir / "events.jsonl").stat().st_size


def test_browser_counter(run_dir):
    write(run_dir, ev("session", action="provisioning"), ev("session", action="open", session_id="a"),
          ev("session", action="provisioning"), ev("session", action="open", session_id="b"),
          ev("session", action="provisioning"))
    v = RunView(run_dir)
    b = v.snapshot()["browsers"]
    assert (b["open"], b["provisioning"]) == (2, 1)
    write(run_dir, ev("session", action="close", session_id="a"),
          {"t": "session", "ts": "x", "action": "close", "session_id": "b", "source": "dawg-replay"},
          ev("log", level="info", msg="страховка: освобождён браузер b", source="dawg-replay"))
    s = v.snapshot()
    assert s["browsers"]["open"] == 0
    assert s["log"][-1]["source"] == "dawg-replay"


def test_broken_and_unknown_lines_go_to_log_as_raw(run_dir):
    write(
        run_dir,
        ev("stage", name="A"),
        "не json",
        '["массив"]',
        '{"нет": "t"}',
        ev("chart", data=[1, 2]),
        ev("progress", id="p", done="много"),  # плохое поле
        ev("result", status="странный", summary="x"),
        ev("session", action="teleport"),
        ev("rows", table="t", rows="не список"),
        "",
        ev("stage", name="B"),
    )
    s = RunView(run_dir).snapshot()
    raw = [e for e in s["log"] if e["level"] == "raw"]
    assert len(raw) == 8
    assert raw[0]["msg"] == "не json" and raw[0]["note"] == "битая строка" and raw[0]["line"] == 2
    assert "неизвестный тип события `chart`" in raw[3]["note"]
    assert s["stage"] == "B" and s["result"] is None and s["progress"] == {}


def test_partial_last_line_waits(run_dir):
    line = json.dumps(ev("stage", name="Длинный этап"), ensure_ascii=False).encode("utf-8")
    write(run_dir, ev("stage", name="A"), line[:15])
    v = RunView(run_dir)
    s = v.snapshot()
    assert s["stage"] == "A" and not any(e["level"] == "raw" for e in s["log"])
    # Обрыв посреди многобайтного символа тоже ждёт.
    cut = line.index("Д".encode()) + 1
    write(run_dir, line[15:cut])
    assert v.poll() == [] and v.stage == "A"
    write(run_dir, line[cut:] + b"\n")
    changes = v.poll()
    assert [c.kind for c in changes] == ["stage"] and v.stage == "Длинный этап"


def test_rows_limit_keeps_first(run_dir):
    for i in range(5):
        write(run_dir, ev("rows", table="t", rows=[{"n": i * 3 + k} for k in range(3)]))
    v = RunView(run_dir, rows_limit=7)
    t = v.snapshot()["tables"]["t"]
    assert [r["n"] for r in t["rows"]] == list(range(7))
    assert (t["total"], t["shown"], t["truncated"]) == (15, 7, True)
    write(run_dir, ev("rows", table="t", rows=[{"n": 99}]))
    c = v.poll()[-1]
    assert c.kind == "rows" and c.data["added"] == [] and c.data["total"] == 16


def test_artifact_checks(run_dir):
    (run_dir / "output" / "sub").mkdir()
    (run_dir / "output" / "sub" / "a.json").write_text("{}", encoding="utf-8")
    (run_dir / "secret.txt").write_text("x", encoding="utf-8")
    write(
        run_dir,
        ev("artifact", path="sub/a.json", label="JSON"),
        ev("artifact", path="../secret.txt", label="наружу"),
        ev("artifact", path="/etc/passwd", label="абсолютный"),
        ev("artifact", path="missing.csv", label="нет файла"),
        ev("artifact", path="sub/a.json", label="JSON v2"),
    )
    s = RunView(run_dir).snapshot()
    assert [(a["path"], a["label"], a["exists"]) for a in s["artifacts"]] == [
        ("missing.csv", "нет файла", False),
        ("sub/a.json", "JSON v2", True),
    ]
    warns = [e["msg"] for e in s["log"] if e["level"] == "warn"]
    assert len(warns) == 2 and all("вне каталога output" in w for w in warns)


def test_log_limit_keeps_last(run_dir):
    write(run_dir, *[ev("log", level="info", msg=str(i)) for i in range(50)])
    s = RunView(run_dir, log_limit=10).snapshot()
    assert [e["msg"] for e in s["log"]] == [str(i) for i in range(40, 50)] and s["log_total"] == 50


def test_unknown_log_level_shown_as_info(run_dir):
    write(run_dir, ev("log", level="fatal", msg="x"))
    assert RunView(run_dir).snapshot()["log"][0]["level"] == "info"


def test_no_file_yet(run_dir):
    v = RunView(run_dir)
    assert v.snapshot()["stage"] is None and v.poll() == []
    write(run_dir, ev("stage", name="A"))
    assert [c.kind for c in v.poll()] == ["stage"]


def test_changes_since_seq_and_resync(run_dir):
    write(run_dir, ev("stage", name="A"), ev("log", level="info", msg="1"))
    v = RunView(run_dir, history=5)
    snap = v.snapshot()
    write(run_dir, ev("stage", name="B"))
    batch = v.changes(snap["seq"])
    assert not batch["resync"] and [c["kind"] for c in batch["changes"]] == ["stage"]
    assert v.changes(batch["seq"])["changes"] == []
    write(run_dir, *[ev("log", level="info", msg=str(i)) for i in range(10)])
    assert v.changes(batch["seq"])["resync"] is True  # отстал дальше истории
    assert v.changes(v.seq)["changes"] == []


def test_truncated_file_resets(run_dir):
    write(run_dir, ev("stage", name="A"), ev("stage", name="B"))
    v = RunView(run_dir)
    seq = v.snapshot()["seq"]
    write(run_dir, ev("stage", name="C"), mode="w")
    assert v.changes(seq)["resync"] is True
    s = v.snapshot()
    assert [t["name"] for t in s["timeline"]] == ["C"]


def test_incremental_read_of_big_file(run_dir):
    """Дочитывание, а не перечитывание: poll читает только новые байты."""
    write(run_dir, *[ev("progress", id="p", label="P", done=i, total=10**6) for i in range(100_000)])
    v = RunView(run_dir)
    t0 = time.perf_counter()
    v.snapshot()
    first = time.perf_counter() - t0
    size = (run_dir / "events.jsonl").stat().st_size
    assert v.position == size and v.progress["p"]["done"] == 99_999

    reads = []
    real_open = open

    def counting_open(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        if str(path).endswith("events.jsonl"):
            real_read = f.read
            f.read = lambda n=-1: (reads.append(r := real_read(n)), r)[1]
        return f

    import builtins

    builtins_open = builtins.open
    builtins.open = counting_open
    try:
        write_line = json.dumps(ev("stage", name="Хвост")) + "\n"
        with real_open(run_dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(write_line)
        t0 = time.perf_counter()
        changes = v.poll()
        tail = time.perf_counter() - t0
    finally:
        builtins.open = builtins_open
    assert [c.kind for c in changes] == ["stage"]
    from dawg_replay.runview import HEAD_BYTES

    # Прочитаны только хвост и начало файла (проверка, что файл не подменили).
    assert sum(len(r) for r in reads) == len(write_line.encode("utf-8")) + HEAD_BYTES
    assert tail < first / 10
    assert v.poll() == []  # без новых байт ничего не читается


def test_follow_until_finished(run_dir):
    state = {"status": "running"}
    v = RunView(run_dir, run_info=lambda: dict(state))
    snap = v.snapshot()
    assert snap["run"] == {"status": "running"}
    got = []

    def consume():
        for batch in v.follow(after=snap["seq"], interval=0.02, idle_after_finish=0.1):
            got.append(batch)

    t = threading.Thread(target=consume)
    t.start()
    write(run_dir, ev("stage", name="A"))
    time.sleep(0.1)
    write(run_dir, ev("result", status="ok", summary="готово"))
    state["status"] = "ok"
    t.join(5)
    assert not t.is_alive()
    kinds = [c["kind"] for b in got for c in b["changes"]]
    assert kinds.count("stage") == 1 and "result" in kinds and "run" in kinds


def test_follow_stops_on_event(run_dir):
    v = RunView(run_dir, run_info=lambda: {"status": "running"})
    stop = threading.Event()
    stop.set()
    assert list(v.follow(stop=stop)) == []


def test_for_run_with_app(app, home):
    rid = "20261114-120000-abcdef"
    app.runs.reserve(run_id=rid, script_name="s", script_version=1, script_path="/x", draft=False,
                     preset=None, params={}, timeout_seconds=60, limit=10)
    run_dir = app.paths.run_dir(rid)
    (run_dir / "output").mkdir(parents=True)
    write(run_dir, ev("stage", name="A"))
    (run_dir / "insurance.json").write_text(json.dumps({"suspects": [{"session_id": "z", "started_at": "t"}]}), encoding="utf-8")
    app.set_config("rows_preview_limit", "3")
    v = RunView.for_run(app, rid)
    s = v.snapshot()
    assert v.rows_limit == 3
    assert s["run"]["status"] == "running" and s["run"]["id"] == rid
    assert [x["session_id"] for x in s["run"]["suspects"]] == ["z"]
    app.runs.finish(rid, status="ok", reason=None, summary="готово")
    c = v.poll()
    assert [x.kind for x in c] == ["run"] and c[0].data["label"] == "ok" and c[0].data["summary"] == "готово"
    assert v.finished


def test_same_size_rewrite_resets(run_dir):
    write(run_dir, ev("stage", name="A"))
    v = RunView(run_dir)
    seq = v.snapshot()["seq"]
    write(run_dir, ev("stage", name="Z"), mode="w")  # тот же размер, другое содержимое
    assert v.changes(seq)["resync"] is True
    assert v.snapshot()["stage"] == "Z"
