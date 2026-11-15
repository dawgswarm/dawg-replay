"""kit.item: отчёт по строкам входа (контракт 1.2)."""

import io

import pytest

from dawg_replay_kit.events import ITEM_MSG_MAX, ConsoleSink, Emitter


class ListSink:
    def __init__(self):
        self.events = []

    def write(self, e):
        self.events.append(e)

    def close(self):
        pass


def items(sink):
    return [{k: v for k, v in e.items() if k != "ts"} for e in sink.events if e["t"] == "item"]


def test_item_fields_and_masking():
    sink = ListSink()
    em = Emitter(sink, secrets=["pass-123"])
    em.item("a@b.ru", "ok", row=2)
    em.item(7, "failed", RuntimeError("неверный пароль pass-123"), row=3)
    em.item("c", "skipped", "уже сделано")
    assert items(sink) == [
        {"t": "item", "key": "a@b.ru", "status": "ok", "row": 2},
        {"t": "item", "key": 7, "status": "failed", "msg": "неверный пароль ***", "row": 3},
        {"t": "item", "key": "c", "status": "skipped", "msg": "уже сделано"},
    ]


def test_item_long_message_cut():
    sink = ListSink()
    Emitter(sink).item("k", "failed", "x" * 5000)
    msg = items(sink)[0]["msg"]
    assert len(msg) == ITEM_MSG_MAX and msg.endswith("…")


@pytest.mark.parametrize("key, status, row, exc", [
    (None, "ok", None, TypeError),
    (True, "ok", None, TypeError),
    ("  ", "ok", None, ValueError),
    ("k", "done", None, ValueError),
    ("k", "ok", "2", TypeError),
    ("k", "ok", True, TypeError),
])
def test_item_validation(key, status, row, exc):
    with pytest.raises(exc):
        Emitter(ListSink()).item(key, status, row=row)


def test_item_console_format():
    out = io.StringIO()
    sink = ConsoleSink(out)
    Emitter(sink).item("a@b.ru", "failed", "нет кнопки", row=4)
    assert out.getvalue().rstrip().endswith("failed  a@b.ru (строка 4): нет кнопки")


def test_kit_item_is_exported():
    import dawg_replay_kit as kit

    assert "item" in kit.__all__ and callable(kit.item)


def test_console_item_label_without_repeated_row():
    import io

    from dawg_replay_kit.events import ConsoleSink

    sink = ConsoleSink(io.StringIO())
    assert sink.format({"t": "item", "ts": "", "key": 2, "status": "ok", "row": 2, "msg": "готово"}).endswith("ok      строка 2: готово")
    assert sink.format({"t": "item", "ts": "", "key": "a@b.ru", "status": "ok", "row": 4}).endswith("ok      a@b.ru (строка 4)")
    assert sink.format({"t": "item", "ts": "", "key": "a@b.ru", "status": "ok"}).endswith("ok      a@b.ru")
