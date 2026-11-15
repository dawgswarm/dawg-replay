"""kit.ask: вопрос человеку посреди запуска (docs/contract.md, раздел 8)."""

import io
import json

import pytest

from dawg_replay_kit.ask import AskTimeout, Asker
from dawg_replay_kit.events import ConsoleSink, Emitter


class ListSink:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)

    def close(self):
        pass


class Clock:
    """Ручные часы: sleep двигает время и даёт тесту подложить ответ."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.on_sleep = on_sleep
        self.sleeps = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def make(tmp_path, on_sleep=None, secrets=()):
    sink = ListSink()
    events = Emitter(sink, secrets=secrets, auto_flush=False)
    clock = Clock(on_sleep)
    asker = Asker(events, tmp_path / "answers", clock=clock, sleep=clock.sleep)
    return asker, sink, clock


def put_answer(tmp_path, sink, answer):
    ask = [e for e in sink.events if e["t"] == "ask"][-1]
    d = tmp_path / "answers"
    d.mkdir(exist_ok=True)
    (d / f"{ask['id']}.json").write_text(json.dumps({"id": ask["id"], "answer": answer}), encoding="utf-8")
    return d / f"{ask['id']}.json"


def test_text_answer_is_read_and_logged(tmp_path):
    holder = {}
    asker, sink, _ = make(tmp_path, lambda n: n == 3 and holder.setdefault("p", put_answer(tmp_path, sink, " Казань ")))
    assert asker.ask("Какой склад?", timeout_minutes=5) == "Казань"
    ask, closed = [e for e in sink.events if e["t"] in ("ask", "ask_closed")]
    assert (ask["question"], ask["kind"], ask["secret"]) == ("Какой склад?", "text", False)
    assert "choices" not in ask and ask["deadline"].endswith("Z")
    assert closed == {**closed, "id": ask["id"], "outcome": "answered", "answer": "Казань"}
    assert holder["p"].exists()  # обычный ответ остаётся в каталоге запуска


def test_secret_answer_deleted_and_masked(tmp_path):
    holder = {}
    asker, sink, _ = make(tmp_path, lambda n: n == 1 and holder.setdefault("p", put_answer(tmp_path, sink, "482913")))
    code = asker.ask("Код из SMS", secret=True, timeout_minutes=10)
    assert code == "482913"
    assert not holder["p"].exists()
    closed = sink.events[-1]
    assert closed["t"] == "ask_closed" and "answer" not in closed
    asker._events.log("info", f"ввели код {code}")
    assert sink.events[-1]["msg"] == "ввели код ***"
    assert "482913" not in json.dumps(sink.events, ensure_ascii=False)


@pytest.mark.parametrize("raw, value", [("yes", True), ("нет", False), (True, True), ("Да", True)])
def test_confirm(tmp_path, raw, value):
    asker, sink, _ = make(tmp_path, lambda n: n == 1 and put_answer(tmp_path, sink, raw))
    assert asker.ask("Отправить 12 заявок?", kind="confirm") is value


def test_choice_by_text_or_number_and_bad_answer_waits_for_another(tmp_path):
    answers = {1: "Тула", 3: "2"}

    def feed(n):
        if n in answers:
            put_answer(tmp_path, sink, answers[n])

    asker, sink, _ = make(tmp_path, feed)
    assert asker.ask("Какой склад?", kind="choice", choices=["Москва", "Казань"]) == "Казань"
    ask = [e for e in sink.events if e["t"] == "ask"][0]
    assert ask["choices"] == ["Москва", "Казань"]
    assert any(e["t"] == "log" and "не подошёл" in e["msg"] for e in sink.events)


def test_timeout_raises_and_closes_question(tmp_path):
    asker, sink, clock = make(tmp_path)
    with pytest.raises(AskTimeout, match="не дождались ответа на вопрос «Код\\?» за 0,5 мин"):
        asker.ask("Код?", timeout_minutes=0.5)
    assert sink.events[-1]["outcome"] == "timeout"
    assert clock.now == pytest.approx(30, abs=0.3)


def test_ids_are_unique_and_safe(tmp_path):
    asker, sink, _ = make(tmp_path)
    for _ in range(2):
        with pytest.raises(AskTimeout):
            asker.ask("Код?", timeout_minutes=0.01)
    ids = [e["id"] for e in sink.events if e["t"] == "ask"]
    assert len(set(ids)) == 2 and all(i.replace("-", "").isalnum() for i in ids)


@pytest.mark.parametrize(
    "kw",
    [
        {"question": ""},
        {"question": "q", "kind": "number"},
        {"question": "q", "kind": "choice"},
        {"question": "q", "kind": "choice", "choices": ["a", "a"]},
        {"question": "q", "kind": "choice", "choices": "ab"},
        {"question": "q", "choices": ["a"]},
        {"question": "q", "kind": "confirm", "secret": True},
        {"question": "q", "timeout_minutes": 0},
        {"question": "q", "timeout_minutes": True},
    ],
)
def test_bad_arguments(tmp_path, kw):
    asker, sink, _ = make(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        asker.ask(**kw)
    assert sink.events == []


def test_terminal_mode_input_and_getpass():
    sink = ListSink()
    out = io.StringIO()
    lines = iter(["может быть", "да"])
    secret_calls = []
    asker = Asker(Emitter(sink, auto_flush=False), None, input_fn=lambda: next(lines),
                  secret_input_fn=lambda: secret_calls.append(1) or "1234", stream=out)
    assert asker.ask("Отправить?", kind="confirm") is True
    assert asker.ask("Код", secret=True) == "1234" and secret_calls == [1]
    choice = Asker(Emitter(sink, auto_flush=False), None, input_fn=lambda: "2", stream=out)
    assert choice.ask("Склад?", kind="choice", choices=["Москва", "Казань"]) == "Казань"
    text = out.getvalue()
    assert "ВОПРОС: Отправить?" in text and "Ответ не подошёл" in text and "  2. Казань" in text
    assert sink.events == []  # в терминале вопрос не событие


def test_terminal_mode_closed_input_is_ask_timeout():
    def eof():
        raise EOFError

    asker = Asker(Emitter(ListSink(), auto_flush=False), None, input_fn=eof, stream=io.StringIO())
    with pytest.raises(AskTimeout, match="ввод закрыт"):
        asker.ask("Код?")


def test_console_format_of_ask_events():
    sink = ConsoleSink(io.StringIO())
    line = sink.format({"t": "ask", "ts": "2026-11-15T10:00:00.000Z", "id": "1-a", "question": "Склад?",
                        "kind": "choice", "choices": ["Москва", "Казань"], "deadline": "2026-11-15T10:30:00Z"})
    assert line.endswith("ВОПРОС: Склад? (Москва / Казань), ответить до 10:30")
    assert sink.format({"t": "ask_closed", "ts": "", "outcome": "answered", "answer": False}).endswith("ответ получен: нет")
    assert sink.format({"t": "ask_closed", "ts": "", "outcome": "answered", "answer": "Москва"}).endswith("ответ получен: Москва")
    assert sink.format({"t": "ask_closed", "ts": "", "outcome": "timeout"}).endswith("ответа на вопрос не дождались")


def test_runtime_answers_dir_from_env(script_file, tmp_path):
    from dawg_replay_kit.runtime import Kit

    script = script_file()
    params = tmp_path / "params.json"
    params.write_text('{"query": "x"}', encoding="utf-8")
    base = {"DAWG_SCRIPT_PARAMS": str(params), "DAWG_SCRIPT_EVENTS": str(tmp_path / "run" / "events.jsonl"),
            "DAWG_SCRIPT_OUTPUT_DIR": str(tmp_path / "out")}
    (tmp_path / "run").mkdir()
    k = Kit.from_environment(script, [], {**base, "DAWG_SCRIPT_ANSWERS_DIR": str(tmp_path / "a")})
    assert k.asker._dir == tmp_path / "a"
    k.events.close()
    k = Kit.from_environment(script, [], base)
    assert k.asker._dir == tmp_path / "run" / "answers"
    k.events.close()
    k = Kit.from_environment(script, ["--query", "x"], {}, stream=io.StringIO(), cwd=tmp_path)
    assert k.asker._dir is None


def test_secret_file_removed_only_after_ask_closed(tmp_path):
    # Иначе между удалением и ask_closed вопрос снова выглядит открытым и приложение примет второй ответ.
    seen = {}

    class Watch(ListSink):
        def write(self, event):
            super().write(event)
            if event["t"] == "ask_closed":
                seen["exists"] = (tmp_path / "answers" / f"{event['id']}.json").exists()

    sink = Watch()
    clock = Clock(lambda n: n == 1 and put_answer(tmp_path, sink, "482913"))
    asker = Asker(Emitter(sink, auto_flush=False), tmp_path / "answers", clock=clock, sleep=clock.sleep)
    assert asker.ask("Код", secret=True) == "482913"
    assert seen == {"exists": True}
    assert list((tmp_path / "answers").iterdir()) == []
