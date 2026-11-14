import io
import json
import re

import pytest

from dawg_replay_kit.events import THROTTLE_SECONDS, ConsoleSink, Emitter, JsonlSink


class ListSink:
    def __init__(self):
        self.events = []
        self.closed = False

    def write(self, event):
        self.events.append(event)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def em(sink, clock, tmp_path):
    # Ручные часы: таймер отключён, отложенное уходит только по событиям (это проверяется ниже).
    return Emitter(sink, secrets=["s3cret", "tok"], output_dir=tmp_path, clock=clock, auto_flush=False)


def strip_ts(events):
    return [{k: v for k, v in e.items() if k != "ts"} for e in events]


def test_ts_is_iso_utc(em, sink):
    em.stage("Поиск")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", sink.events[0]["ts"])


def test_basic_events(em, sink, tmp_path):
    (tmp_path / "cards.csv").write_text("a,b\n")
    em.stage("Поиск")
    em.log("warn", "мало карточек")
    em.rows("cards", [{"title": "A", "price": 10}], columns=["title", "price"])
    em.artifact("cards.csv", "CSV")
    em.result("partial", "Собрано **10** карточек")
    assert strip_ts(sink.events) == [
        {"t": "stage", "name": "Поиск"},
        {"t": "log", "level": "warn", "msg": "мало карточек"},
        {"t": "rows", "table": "cards", "columns": ["title", "price"], "rows": [{"title": "A", "price": 10}]},
        {"t": "artifact", "path": "cards.csv", "label": "CSV", "mime": "text/csv"},
        {"t": "result", "status": "partial", "summary": "Собрано **10** карточек"},
    ]


def test_optional_fields_omitted(em, sink):
    em.progress("p", "Страницы", 3)
    em.metric("m", "Цена", 10)
    em.rows("t", [])
    assert strip_ts(sink.events) == [
        {"t": "progress", "id": "p", "label": "Страницы", "done": 3},
        {"t": "metric", "key": "m", "label": "Цена", "value": 10},
        {"t": "rows", "table": "t", "rows": []},
    ]


def test_progress_throttled_per_id(em, sink, clock):
    for i in range(10):
        em.progress("a", "A", i, 100)
        em.progress("b", "B", i, 100)
        clock.now += 0.01
    # По первому значению на каждый id, остальные отложены.
    assert [(e["id"], e["done"]) for e in sink.events] == [("a", 0), ("b", 0)]
    clock.now = 0.3
    em.progress("a", "A", 50, 100)
    # Отложенное по b ушло, новое по a сразу, так как окно истекло.
    assert [(e["id"], e["done"]) for e in sink.events[2:]] == [("b", 9), ("a", 50)]


def test_progress_last_value_not_lost(em, sink, clock):
    em.progress("a", "A", 1, 10)
    em.progress("a", "A", 2, 10)
    em.progress("a", "A", 3, 10)
    em.flush()
    assert [e["done"] for e in sink.events] == [1, 3]


def test_pending_goes_out_with_other_events(em, sink, clock):
    em.metric("m", "M", 1)
    em.metric("m", "M", 2)
    clock.now = 1
    em.log("info", "x")
    assert [e["t"] for e in sink.events] == ["metric", "metric", "log"]
    assert sink.events[1]["value"] == 2


def test_progress_finished_not_throttled(em, sink):
    em.progress("a", "A", 1, 2)
    em.progress("a", "A", 2, 2)
    assert [e["done"] for e in sink.events] == [1, 2]


def test_result_flushes_pending(em, sink):
    em.metric("m", "M", 1)
    em.metric("m", "M", 2)
    em.result("ok", "готово")
    assert [(e["t"], e.get("value")) for e in sink.events] == [("metric", 1), ("metric", 2), ("result", None)]


def test_four_per_second(em, sink, clock):
    for i in range(100):
        em.metric("m", "M", i)
        clock.now += 0.01  # 1 секунда вызовов
    assert len(sink.events) == 4


def test_secrets_masked_everywhere(em, sink):
    em.log("error", "ошибка запроса с токеном s3cret")
    em.rows("t", [{"url": "https://x.ru/?key=s3cret", "s3cret": ["tok"]}])
    em.metric("m", "M", "tokens")
    dumped = json.dumps(sink.events, ensure_ascii=False)
    assert "s3cret" not in dumped
    assert sink.events[0]["msg"] == "ошибка запроса с токеном ***"
    assert sink.events[1]["rows"][0] == {"url": "https://x.ru/?key=***", "***": ["***"]}


def test_masking_longest_first(sink):
    em = Emitter(sink, secrets=["abc", "abcdef"])
    em.log("info", "abcdef")
    assert sink.events[0]["msg"] == "***"


def test_non_json_values_become_strings(em, sink, tmp_path):
    from datetime import date
    from decimal import Decimal

    f = tmp_path / "e.jsonl"
    j = Emitter(JsonlSink(f))
    j.rows("t", [{"d": date(2026, 1, 2), "p": Decimal("1.50")}])
    j.close()
    assert json.loads(f.read_text(encoding="utf-8"))["rows"] == [{"d": "2026-01-02", "p": "1.50"}]


@pytest.mark.parametrize(
    "call, exc",
    [
        (lambda e: e.log("fatal", "x"), ValueError),
        (lambda e: e.result("done", "x"), ValueError),
        (lambda e: e.rows("t", [1]), TypeError),
        (lambda e: e.artifact("nope.csv", "x"), FileNotFoundError),
        (lambda e: e.artifact("/etc/hosts", "x"), ValueError),
        (lambda e: e.artifact("../x.csv", "x"), ValueError),
        (lambda e: e.session("open"), ValueError),
    ],
)
def test_bad_calls(em, call, exc):
    with pytest.raises(exc):
        call(em)


def test_artifact_absolute_inside_output(em, sink, tmp_path):
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "data.json"
    f.write_text("{}")
    em.artifact(f, "JSON")
    assert sink.events[0]["path"] == "sub/data.json"
    assert sink.events[0]["mime"] == "application/json"


def test_session_events(em, sink):
    em.session("provisioning")
    em.session("open", "sid-1", "b-1")
    em.session("close", "sid-1")
    assert strip_ts(sink.events) == [
        {"t": "session", "action": "provisioning"},
        {"t": "session", "action": "open", "session_id": "sid-1", "browser_id": "b-1"},
        {"t": "session", "action": "close", "session_id": "sid-1"},
    ]


def test_close_flushes_and_stops(em, sink):
    em.metric("m", "M", 1)
    em.metric("m", "M", 2)
    em.close()
    em.log("info", "после закрытия")
    assert [e.get("value") for e in sink.events] == [1, 2]
    assert sink.closed


def test_jsonl_lines(tmp_path):
    f = tmp_path / "events.jsonl"
    em = Emitter(JsonlSink(f))
    em.stage("A")
    em.log("info", "строка\nвторая")
    em.close()
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["msg"] == "строка\nвторая"
    assert "строка" in lines[1]  # без \u-экранирования


def test_console_format(tmp_path):
    out = io.StringIO()
    (tmp_path / "a.csv").write_text("x")
    em = Emitter(ConsoleSink(out, tmp_path), secrets=["s3cret"], output_dir=tmp_path)
    em.stage("Поиск")
    em.log("info", "ключ s3cret")
    em.progress("p", "Страницы", 2, 5)
    em.metric("m", "Цена", 10, "руб")
    em.rows("cards", [{"a": 1}])
    em.artifact("a.csv", "CSV")
    em.result("ok", "готово")
    text = out.getvalue()
    assert "==> Поиск" in text
    assert "INFO  ключ ***" in text
    assert "Страницы: 2/5" in text
    assert "Цена: 10 руб" in text
    assert "таблица cards: +1 строк" in text
    assert f"файл «CSV»: {tmp_path / 'a.csv'}" in text
    assert "ИТОГ ok: готово" in text
    assert "s3cret" not in text
    assert all(re.match(r"\[\d\d:\d\d:\d\d\] ", line) for line in text.splitlines())


# --- отложенное значение уходит по таймеру ------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def read_jsonl(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_pending_progress_goes_out_by_timer_without_events(tmp_path):
    f = tmp_path / "events.jsonl"
    em = Emitter(JsonlSink(f))
    em.progress("p", "Страницы", 1, 10)
    em.progress("p", "Страницы", 2, 10)  # в окне прореживания: отложено
    em.metric("m", "Карточки", 5)
    em.metric("m", "Карточки", 7)
    assert [e.get("done", e.get("value")) for e in read_jsonl(f)] == [1, 5]
    time.sleep(0.4)  # скрипт ждёт страницу, событий нет
    assert [e.get("done", e.get("value")) for e in read_jsonl(f)] == [1, 5, 2, 7]
    em.close()


def test_timer_fires_close_to_window(tmp_path):
    f = tmp_path / "events.jsonl"
    em = Emitter(JsonlSink(f))
    em.progress("p", "P", 1, 10)
    t0 = time.monotonic()
    em.progress("p", "P", 2, 10)
    deadline = t0 + 2
    while len(read_jsonl(f)) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    elapsed = time.monotonic() - t0
    assert len(read_jsonl(f)) == 2
    assert 0.2 <= elapsed < 0.6, elapsed  # окно 0.25 с плюс запас на загруженную машину
    em.close()


class TimedSink(JsonlSink):
    """Запоминает момент каждой записи: проверки не зависят от скорости машины."""

    def __init__(self, path):
        super().__init__(path)
        self.times: list[float] = []

    def write(self, event):
        self.times.append(time.monotonic())
        super().write(event)


def test_timer_respects_newer_value_and_window(tmp_path):
    f = tmp_path / "events.jsonl"
    sink = TimedSink(f)
    em = Emitter(sink)
    for i in range(1, 51):  # плотный цикл, на быстрой машине ~0.5 с
        em.progress("p", "P", i, 100)
        time.sleep(0.01)
    deadline = time.monotonic() + 5
    while read_jsonl(f)[-1]["done"] != 50 and time.monotonic() < deadline:
        time.sleep(0.05)
    done = [e["done"] for e in read_jsonl(f)]
    assert done[-1] == 50  # последнее значение таймер записал сам, без close
    assert done == sorted(done) and len(done) < 50  # отложенные значения заменяются новыми
    # Не чаще раза в окно: между соседними записями не меньше окна, сколько бы ни шёл цикл.
    gaps = [b - a for a, b in zip(sink.times, sink.times[1:])]
    assert all(g >= THROTTLE_SECONDS - 0.02 for g in gaps), gaps
    em.close()


def test_close_stops_timer_and_nothing_written_after(tmp_path):
    f = tmp_path / "events.jsonl"
    em = Emitter(JsonlSink(f))
    em.metric("m", "M", 1)
    em.metric("m", "M", 2)
    timer = em._timer
    assert timer is not None and timer.daemon
    em.close()  # отложенное уходит в close; после закрытия таймер ничего не пишет
    assert [e["value"] for e in read_jsonl(f)] == [1, 2]
    timer.join(1)
    assert not timer.is_alive()
    em.metric("m", "M", 3)
    time.sleep(0.3)
    assert [e["value"] for e in read_jsonl(f)] == [1, 2]


def test_close_races_with_timer(tmp_path):
    # Закрытие ровно на границе окна много раз подряд: без исключений и без записи после закрытия.
    for i in range(30):
        f = tmp_path / f"e{i}.jsonl"
        sink = JsonlSink(f)
        em = Emitter(sink, throttle=0.01)
        em.progress("p", "P", 1, 10)
        em.progress("p", "P", 2, 10)
        time.sleep(0.01)
        em.close()
        assert [e["done"] for e in read_jsonl(f)] == [1, 2]


def test_concurrent_updates_from_threads(tmp_path):
    f = tmp_path / "events.jsonl"
    em = Emitter(JsonlSink(f), throttle=0.05)

    def work(n):
        for i in range(1, 101):
            em.progress(f"p{n}", "P", i, 1000)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.2)
    events = read_jsonl(f)
    last = {}
    for e in events:
        last[e["id"]] = e["done"]
    assert last == {f"p{n}": 100 for n in range(4)}
    em.close()


SIGTERM_SCRIPT = """
import os, signal, sys, time
from pathlib import Path
from dawg_replay_kit import cancel
from dawg_replay_kit.events import Emitter, JsonlSink

em = Emitter(JsonlSink(sys.argv[1]))
cancel.install()
try:
    em.progress("p", "P", 1, 10)
    em.progress("p", "P", 2, 10)
    time.sleep(0.4)          # таймер уже отправил 2
    em.progress("p", "P", 3, 10)  # окно после таймерной отправки 2 ещё идёт: отложено
    em.progress("p", "P", 4, 10)  # заменяет 3; сигнал приходит раньше таймера
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)
finally:
    em.log("info", "finally")
    em.close()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM самому себе проверяется на POSIX")
def test_sigterm_exit_with_timer(tmp_path):
    f = tmp_path / "events.jsonl"
    p = subprocess.run([sys.executable, "-c", SIGTERM_SCRIPT, str(f)], capture_output=True, text=True, timeout=20)
    assert p.returncode == 143, p.stderr
    assert "Traceback" not in p.stderr and "Exception" not in p.stderr, p.stderr
    events = read_jsonl(f)
    assert [e.get("done") for e in events if e["t"] == "progress"] == [1, 2, 4]
    assert "finally" in [e.get("msg") for e in events]
