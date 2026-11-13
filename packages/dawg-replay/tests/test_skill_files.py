"""Справочные файлы скилла dawg-replay-builder: template.py и exploration.md."""

import csv
import importlib.util
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from dawg_replay_kit.manifest import load_manifest, parse_manifest

from dawg_replay.cli import main

SKILL = Path(__file__).resolve().parents[3] / "skill" / "skills" / "dawg-replay-builder"
TEMPLATE = SKILL / "template.py"
EXPLORATION = SKILL / "exploration.md"


class FakeLog:
    def __init__(self):
        self.lines = []

    def warn(self, msg):
        self.lines.append(("warn", msg))

    info = error = warn


@pytest.fixture
def tpl(monkeypatch):
    spec = importlib.util.spec_from_file_location("skill_template", TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # импорт ничего не запускает: kit поднимается лениво
    module.kit = SimpleNamespace(log=FakeLog())
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return module


def test_template_passes_validate(capsys):
    assert main(["validate", str(TEMPLATE)]) == 0
    m = load_manifest(TEMPLATE)
    assert {p.type for p in m.params} >= {"string", "int", "enum", "bool"}
    assert "dawg-replay-kit>=1,<2" in m.dependencies


def test_retries_and_captcha(tpl):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("медленно")
        return "ok"

    assert tpl.with_retries("первая страница", flaky, attempts=3, pause=0) == "ok"
    assert len(calls) == 3 and len(tpl.kit.log.lines) == 2

    def captcha():
        calls.append(1)
        raise tpl.Captcha("капча")

    calls.clear()
    with pytest.raises(tpl.Captcha):
        tpl.with_retries("x", captcha, attempts=3, pause=0)
    assert len(calls) == 1  # капчу не повторяем

    with pytest.raises(RuntimeError, match="не получилось за 2 попытки"):
        tpl.with_retries("x", lambda: (_ for _ in ()).throw(ValueError("нет")), attempts=2, pause=0)


class FakePage:
    def __init__(self, script):
        self.script = list(script)
        self.waits = 0

    def evaluate(self, js, arg=None):
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step

    def wait_for_timeout(self, ms):
        self.waits += 1


def test_wait_ready(tpl):
    state = json.dumps({"items": [{"id": 1}, {"id": 2}]})
    page = FakePage([
        ("Antibot Challenge Page", None),  # JS-проверка идёт
        RuntimeError("Execution context was destroyed"),  # страница перезагружается
        ("Выдача", state),
    ])
    assert [i["id"] for i in tpl.wait_ready(page, timeout=5)] == [1, 2]
    with pytest.raises(tpl.Captcha):
        tpl.wait_ready(FakePage([("Antibot Captcha", None)]), timeout=5)
    with pytest.raises(TimeoutError):
        tpl.wait_ready(FakePage([("Выдача", None)]), timeout=0.01)


def test_scroll_until(tpl):
    pages = {}
    wheels = []

    class Page:
        mouse = SimpleNamespace(wheel=lambda x, y: (wheels.append(y), len(wheels) == 2 and pages.update({2: [{"id": 9}]})))

        def wait_for_timeout(self, ms):
            pass

    assert tpl.scroll_until(Page(), 2, pages, timeout=5) == [{"id": 9}]
    assert len(wheels) == 2 and 2 not in pages
    with pytest.raises(TimeoutError):
        tpl.scroll_until(Page(), 3, {}, timeout=0)


def test_page_number_and_row(tpl):
    assert tpl.page_number("https://x.ru/api/search?q=a&page=3", {}) == 3
    assert tpl.page_number("https://x.ru/api/search?q=a", {"page": 4}) == 4
    assert tpl.page_number("https://x.ru/api/search", {}) == 1
    row = tpl.card_row({"id": 7, "name": "Чайник", "price": 267, "url": "/product/7/?at=track"}, 1)
    assert row["url"] == tpl.ORIGIN + "/product/7/" and list(row) == tpl.COLUMNS


def test_collector(tpl, tmp_path):
    path = tmp_path / "cards.csv"
    c = tpl.Collector(path)
    keep = lambda r: r["price"] is not None
    assert len(c.add([{"id": 1, "price": 10}, {"id": 2, "price": None}, {"id": 3, "price": 5}], keep)) == 2
    assert len(c.add([{"id": 1, "price": 10}, {"id": 4, "price": 1}, {"name": "без id"}], keep)) == 1
    c.close()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert [(r["position"], r["id"]) for r in rows] == [("1", "1"), ("2", "3"), ("3", "4")]


def test_exploration_probe_script_is_valid():
    text = EXPLORATION.read_text(encoding="utf-8")
    probe = re.search(r"```python\n(.*?)```", text, re.S).group(1)
    assert parse_manifest(probe).name == "probe"
    compile(probe, "probe.py", "exec")
    assert "browser_release" in text and "browser_provision" in text


@pytest.mark.parametrize("path", [TEMPLATE, EXPLORATION])
def test_no_long_dashes(path):
    assert "—" not in path.read_text(encoding="utf-8")
