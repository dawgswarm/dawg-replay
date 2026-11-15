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
    assert "dawg-replay-kit>=1.1,<2" in m.dependencies
    assert m.contract_minor == 1 and m.site and m.tags


def test_retries_and_site_check(tpl):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("медленно")
        return "ok"

    assert tpl.with_retries("первая страница", flaky, attempts=3, pause=0) == "ok"
    assert len(calls) == 3 and len(tpl.kit.log.lines) == 2

    def stuck():
        calls.append(1)
        raise tpl.SiteCheckTimeout("сайт не пропустил браузер")

    calls.clear()
    with pytest.raises(tpl.SiteCheckTimeout, match="не пропустил"):
        tpl.with_retries("x", stuck, attempts=3, pause=0)
    assert len(calls) == 3  # проверку повторяем, но не больше attempts

    def check_then_ok():
        calls.append(1)
        if len(calls) == 1:
            raise tpl.SiteCheckTimeout("сайт не пропустил браузер")
        return "ok"

    calls.clear()
    assert tpl.with_retries("x", check_then_ok, attempts=3, pause=0) == "ok"

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
    # проверка с капчей тоже ждётся: браузер проходит её сам, данные появляются
    page = FakePage([("Antibot Captcha", None), ("Antibot Captcha", None), ("Выдача", state)])
    assert len(tpl.wait_ready(page, timeout=5)) == 2 and page.waits == 2
    for title in ("Antibot Captcha", "Antibot Challenge Page"):
        with pytest.raises(tpl.SiteCheckTimeout, match="страница проверки не прошла за 0 с"):
            tpl.wait_ready(FakePage([(title, None)]), timeout=0.01)
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


# --- template_actions.py: действия по таблице ------------------------------------

ACTIONS = SKILL / "template_actions.py"


def test_actions_template_passes_validate():
    assert main(["validate", str(ACTIONS)]) == 0
    m = load_manifest(ACTIONS)
    (table,) = [p for p in m.params if p.type == "table"]
    assert table.secret and table.required and set(table.columns) >= {"login", "password"}
    assert m.contract_minor == 2 and "dawg-replay-kit>=1.2,<2" in m.dependencies


class FakeSite:
    """Кабинет: аккаунты с паролем и значением настройки, у части спрашивается код."""

    def __init__(self, tpl, accounts, need_code=()):
        self.tpl, self.accounts, self.need_code = tpl, accounts, set(need_code)
        self.sessions: set[str] = set()  # сохранённые входы (как kit.browser(login=...))
        self.saves = 0


class CabinetLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    def count(self):
        return 1 if self.page.visible(self.sel) else 0

    @property
    def first(self):
        return self

    def inner_text(self, timeout=None):
        return "Неверный логин или пароль"


class CabinetPage:
    def __init__(self, site, login):
        self.site, self.login, self.url = site, login, ""
        self.form: dict[str, str] = {}
        self.state = "anon"  # anon, code, error, in
        self.saved_flag = False
        if login in site.sessions:
            self.state = "in"

    def visible(self, sel):
        t = self.site.tpl
        return {t.LOGGED_IN: self.state == "in", t.CODE_FIELD: self.state == "code",
                t.LOGIN_ERROR: self.state == "error", t.SAVED: self.saved_flag}.get(sel, False)

    def locator(self, sel):
        return CabinetLocator(self, sel)

    def goto(self, url, timeout=None):
        self.url, self.saved_flag = url, False

    reload = lambda self, timeout=None: self.goto(self.url)  # noqa: E731

    def fill(self, sel, value):
        self.form[sel] = value

    def click(self, sel):
        t, acc = self.site.tpl, self.site.accounts.get(self.form.get(self.site.tpl.LOGIN_FIELD, self.login))
        if sel == t.SUBMIT and self.state == "code":
            self.state = "in" if self.form.get(t.CODE_FIELD) == "1234" else "error"
        elif sel == t.SUBMIT:
            if not acc or acc["password"] != self.form.get(t.PASSWORD_FIELD):
                self.state = "error"
            else:
                self.state = "code" if self.login in self.site.need_code else "in"
        elif sel == t.SAVE:
            self.site.accounts[self.login]["value"] = self.form[t.VALUE_FIELD]
            self.site.saves += 1
            self.saved_flag = True

    def wait_for_selector(self, sel, timeout=None):
        if not any(self.visible(s.strip()) for s in sel.split(",")):
            raise TimeoutError(sel)

    def input_value(self, sel, timeout=None):
        return self.site.accounts[self.login]["value"]


class AskTimeout(Exception):
    pass


def actions_kit(tpl, site, rows, tmp_path, answers, **params):
    events = []

    class Browser:
        def __init__(self, login, geo=None):
            self.login = login

        def __enter__(self):
            self.login_restored = self.login in site.sessions
            self.page = CabinetPage(site, self.login)
            return self

        def forget_login(self):
            events.append(("forget", self.login))
            self.forgotten = True

        def save_login(self):
            events.append(("save", self.login))
            if self.page.state == "in":
                site.sessions.add(self.login)
            self.forgotten = False
            return True

        def __exit__(self, *exc):
            if self.page.state == "in" and not getattr(self, "forgotten", False):
                site.sessions.add(self.login)

    def ask(question, kind="text", choices=None, secret=False, timeout_minutes=30):
        events.append(("ask", kind, secret))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    defaults = {"confirm_first": True, "check_only": False, "geo": ""}
    kit = SimpleNamespace(
        params=SimpleNamespace(accounts=rows, **{**defaults, **params}),
        output_dir=tmp_path, log=FakeLog(), AskTimeout=AskTimeout, browser=Browser, ask=ask,
        set_browser_limit=lambda n: None, stage=lambda name: None,
        progress=lambda *a: events.append(("progress", a[2], a[3])),
        item=lambda key, status, msg=None, row=None: events.append(("item", key, status, msg, row)),
        artifact=lambda path, *a: events.append(("artifact", path.name)),
        result=lambda status, summary: events.append(("result", status, summary)),
    )
    tpl.kit = kit
    return events


@pytest.fixture
def act(monkeypatch):
    spec = importlib.util.spec_from_file_location("skill_template_actions", ACTIONS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return module


def rows_of(*logins):
    return [{"login": lg, "password": "pw-" + lg, "value": "new", "_row": i + 2} for i, lg in enumerate(logins)]


def test_actions_rows_report_and_idempotent_rerun(act, tmp_path):
    site = FakeSite(act, {"anna": {"password": "pw-anna", "value": "old"},
                          "boris": {"password": "pw-boris", "value": "new"},
                          "vera": {"password": "pw-vera", "value": "old"}}, need_code={"vera"})
    rows = rows_of("anna", "boris", "ghost", "vera")
    events = actions_kit(act, site, rows, tmp_path, [True, "1234"])
    act.main()
    items = [e[1:] for e in events if e[0] == "item"]
    assert items == [
        (2, "ok", "сделано", 2),
        (3, "skipped", "уже сделано", 3),
        (4, "failed", "вход не выполнен: Неверный логин или пароль", 4),
        (5, "ok", "сделано", 5),
    ]
    assert [e for e in events if e[0] == "ask"] == [("ask", "confirm", False), ("ask", "text", True)]
    assert ("forget", "ghost") in events  # неудачный вход не сохранён
    assert events[-1] == ("result", "partial", "из 4: сделано 2, уже было 1, не вышло 1")
    assert not (tmp_path / "report.csv").exists()  # отчёт по строкам строит приложение из kit.item
    assert not any("pw-" in str(e) for e in events)  # паролей в событиях нет

    # Повторный запуск: входы сохранены, всё уже сделано, на сайте ничего не меняется.
    saves = site.saves
    events = actions_kit(act, site, rows_of("anna", "boris", "vera"), tmp_path, [])
    act.main()
    assert [e[2] for e in events if e[0] == "item"] == ["skipped"] * 3 and site.saves == saves
    assert events[-1] == ("result", "ok", "из 3: сделано 0, уже было 3")


def test_actions_not_confirmed_stops_without_changes(act, tmp_path):
    site = FakeSite(act, {"anna": {"password": "pw-anna", "value": "old"}, "boris": {"password": "pw-boris", "value": "old"}})
    events = actions_kit(act, site, rows_of("anna", "boris"), tmp_path, [False])
    with pytest.raises(SystemExit):
        act.main()
    assert site.saves == 0 and [e[2] for e in events if e[0] == "item"] == ["skipped"]
    assert events[-1][:2] == ("result", "failed") and "не подтверждены" in events[-1][2]


def test_actions_check_only_and_code_timeout(act, tmp_path):
    site = FakeSite(act, {"anna": {"password": "pw-anna", "value": "old"}, "vera": {"password": "pw-vera", "value": "old"}},
                    need_code={"vera"})
    events = actions_kit(act, site, rows_of("anna", "vera"), tmp_path, [AskTimeout()], check_only=True)
    act.main()
    items = [e[2:4] for e in events if e[0] == "item"]
    assert items[0] == ("skipped", "нужно сделать (режим проверки, ничего не менялось)")
    assert items[1] == ("failed", "не дождались кода за 10 мин")
    assert site.saves == 0 and events[-1][1] == "partial"
