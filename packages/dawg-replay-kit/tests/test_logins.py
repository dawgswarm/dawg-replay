import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from fake_dawg import KEY, FakeDawg

from dawg_replay_kit import logins
from dawg_replay_kit.browser import Browsers, BrowserSession, Connection
from dawg_replay_kit.dawg_api import AsyncRestProvisioner, BrowserError, RestProvisioner, Sessions
from dawg_replay_kit.events import Emitter
from dawg_replay_kit.logins import LoginError, LoginStore
from dawg_replay_kit.runtime import Kit

POSIX = sys.platform != "win32"
STATE = {
    "cookies": [{"name": "sid", "value": "cookie-secret-123", "domain": ".example.com", "path": "/",
                 "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
    "origins": [{"origin": "https://example.com", "localStorage": [{"name": "token", "value": "ls-secret-456"}]}],
}


class ListSink:
    def __init__(self):
        self.events = []

    def write(self, e):
        self.events.append(e)

    def close(self):
        pass


class FakeRoute:
    def __init__(self, calls):
        self.calls = calls

    def fulfill(self, **kw):
        self.calls.append(("fulfill", kw["status"]))


class FakePage:
    def __init__(self, ctx):
        self.ctx, self.url, self.handler = ctx, None, None

    def route(self, pattern, handler):
        self.handler = handler

    def goto(self, url):
        assert self.handler is not None, "localStorage ставится только при перехваченной сети"
        self.handler(FakeRoute(self.ctx.calls))
        self.url = url

    def evaluate(self, script, items):
        self.ctx.storage.setdefault(self.url, {}).update({i["name"]: i["value"] for i in items})

    def close(self):
        self.ctx.calls.append(("page_close", self.url))


class FakeContext:
    """Контекст Playwright: cookies, localStorage по источникам, storage_state."""

    def __init__(self):
        self.cookies, self.storage, self.calls = [], {}, []
        self.fail_state = False

    def add_cookies(self, cookies):
        self.cookies += cookies

    def new_page(self):
        return FakePage(self)

    def storage_state(self):
        if self.fail_state:
            raise RuntimeError("Target closed")
        return {"cookies": list(self.cookies),
                "origins": [{"origin": o, "localStorage": [{"name": k, "value": v} for k, v in items.items()]}
                            for o, items in self.storage.items()]}


class AsyncPage(FakePage):
    async def route(self, pattern, handler):
        self.handler = handler

    async def goto(self, url):
        r = FakeRoute(self.ctx.calls)

        async def fulfill(**kw):
            FakeRoute.fulfill(r, **kw)

        r.fulfill = fulfill
        await self.handler(r)
        self.url = url

    async def evaluate(self, script, items):
        FakePage.evaluate(self, script, items)

    async def close(self):
        FakePage.close(self)


class AsyncContext(FakeContext):
    async def add_cookies(self, cookies):
        FakeContext.add_cookies(self, cookies)

    async def new_page(self):
        return AsyncPage(self)

    async def storage_state(self):
        return FakeContext.storage_state(self)


@pytest.fixture
def fake():
    f = FakeDawg().start()
    yield f
    f.stop()


def make(fake, logins_dir, ctx_factory=FakeContext):
    sink = ListSink()
    events = Emitter(sink, secrets=[KEY])
    contexts = []

    def connect(ws_url):
        ctx = ctx_factory()
        contexts.append(ctx)
        return Connection(browser="B", context=ctx, page="P", close=lambda: None)

    async def aconnect(ws_url):
        ctx = ctx_factory()
        contexts.append(ctx)

        async def close():
            pass

        return Connection(browser="B", context=ctx, page="P", close=close)

    async def asleep(_):
        await asyncio.sleep(0)

    m = Browsers(
        events, KEY, fake.url,
        provisioner=lambda: RestProvisioner(KEY, fake.url, poll_interval=0.01, sleep=lambda s: None),
        async_provisioner=lambda: AsyncRestProvisioner(KEY, fake.url, poll_interval=0.01, sleep=asleep),
        sessions=lambda: Sessions(KEY, fake.url),
        connect=connect, aconnect=aconnect, logins_dir=logins_dir,
    )
    return m, sink, contexts


def logs(sink):
    return [e["msg"] for e in sink.events if e["t"] == "log"]


# --- хранилище ---


def test_store_roundtrip_and_permissions(tmp_path):
    d = tmp_path / "logins" / "shop"
    store = LoginStore(d)
    assert store.get("a@b.ru") is None and store.list() == []
    store.save("a@b.ru", STATE)
    path = store.path("a@b.ru")
    assert path.parent == d and "a@b" not in path.name and path.name.endswith(".json")
    data = json.loads(path.read_text("utf-8"))
    assert data["name"] == "a@b.ru" and data["state"]["cookies"][0]["value"] == "cookie-secret-123"
    assert data["saved_at"] and data["used_at"] is None
    if POSIX:
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [x.name for x in os.scandir(d)] == [path.name]  # временный файл не остался

    loaded = store.load("a@b.ru")
    assert loaded.state == STATE and loaded.used_at
    info = store.list()[0].info()
    assert info == {"name": "a@b.ru", "saved_at": data["saved_at"], "used_at": loaded.used_at,
                    "cookies": 1, "origins": 1}
    assert "cookie-secret" not in json.dumps(info)


def test_store_merges_origins_and_forgets(tmp_path):
    store = LoginStore(tmp_path)
    store.save("x", STATE)
    prev = store.load("x")
    new = {"cookies": [], "origins": [{"origin": "https://other.ru", "localStorage": [{"name": "a", "value": "1"}]}]}
    login = store.save("x", new, prev)
    assert {o["origin"] for o in login.state["origins"]} == {"https://example.com", "https://other.ru"}
    assert login.state["cookies"] == [] and login.used_at == prev.used_at
    store.save("y", STATE)
    assert [x.name for x in store.list()] == ["x", "y"]
    assert store.forget("x") is True and store.forget("x") is False
    assert store.forget_all() == 1 and store.list() == []


def test_store_skips_broken_files(tmp_path):
    store = LoginStore(tmp_path)
    store.save("ok", STATE)
    (tmp_path / "broken.json").write_text("{", "utf-8")
    (tmp_path / "other.json").write_text('{"name": 1}', "utf-8")
    assert [x.name for x in store.list()] == ["ok"]


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201, None, True, ["a"]])
def test_bad_names(tmp_path, bad):
    with pytest.raises(LoginError):
        LoginStore(tmp_path).path(bad)


def test_number_name_is_string(tmp_path):
    store = LoginStore(tmp_path)
    store.save(12, STATE)
    assert store.get("12").name == "12"


def test_default_dir(tmp_path):
    assert logins.default_dir({"DAWG_SCRIPT_LOGINS_DIR": "/x/y"}, False, tmp_path) == Path("/x/y")
    assert logins.default_dir({}, True, tmp_path) == tmp_path / "logins"
    assert logins.default_dir({}, False, tmp_path) is None


# --- браузер ---


def test_first_run_saves_then_next_run_restores(fake, tmp_path):
    m, sink, ctxs = make(fake, tmp_path)
    with BrowserSession(m, login="a@b.ru") as b:
        assert b.login_restored is False and b.login == "a@b.ru"
        b.context.cookies = list(STATE["cookies"])  # скрипт вошёл
        b.context.storage = {"https://example.com": {"token": "ls-secret-456"}}
    saved = LoginStore(tmp_path).get("a@b.ru")
    assert saved.state == STATE
    assert fake.sessions == {}

    with BrowserSession(m, login="a@b.ru") as b:
        assert b.login_restored is True
        ctx = ctxs[-1]
        assert ctx.cookies == STATE["cookies"]
        assert ctx.storage == {"https://example.com": {"token": "ls-secret-456"}}
        assert ("fulfill", 200) in ctx.calls and ("page_close", "https://example.com") in ctx.calls
    text = json.dumps(sink.events, ensure_ascii=False)
    assert "cookie-secret" not in text and "ls-secret" not in text and "a@b.ru" not in text
    assert "сохранённый вход загружен в браузер" in logs(sink)


def test_forget_login_stops_saving(fake, tmp_path):
    LoginStore(tmp_path).save("u", STATE)
    m, sink, _ = make(fake, tmp_path)
    with BrowserSession(m, login="u") as b:
        assert b.login_restored
        b.forget_login()
    assert LoginStore(tmp_path).get("u") is None
    with BrowserSession(m) as b, pytest.raises(BrowserError):
        b.forget_login()


def test_save_failure_is_warning(fake, tmp_path):
    def broken():
        c = FakeContext()
        c.fail_state = True
        return c

    m, sink, _ = make(fake, tmp_path, broken)
    with BrowserSession(m, login="u"):
        pass
    assert LoginStore(tmp_path).get("u") is None
    assert any("вход не сохранён (RuntimeError)" in x for x in logs(sink))
    assert fake.sessions == {}


def test_restore_failure_is_warning(fake, tmp_path, monkeypatch):
    LoginStore(tmp_path).save("u", STATE)
    m, sink, _ = make(fake, tmp_path)

    def boom(ctx, state):
        raise RuntimeError("cookie-secret-123 rejected")

    monkeypatch.setattr(logins, "restore_sync", boom)
    with BrowserSession(m, login="u") as b:
        assert b.login_restored is False
    assert any("не загрузился в браузер (RuntimeError)" in x for x in logs(sink))
    assert "cookie-secret" not in json.dumps(sink.events)


def test_cancel_during_restore_keeps_old_login(fake, tmp_path, monkeypatch):
    LoginStore(tmp_path).save("u", STATE)
    m, _, _ = make(fake, tmp_path)

    def cancelled(ctx, state):
        raise SystemExit(143)

    monkeypatch.setattr(logins, "restore_sync", cancelled)
    with pytest.raises(SystemExit), BrowserSession(m, login="u"):
        pass
    assert LoginStore(tmp_path).get("u").state == STATE
    assert fake.sessions == {}


def test_no_logins_dir_from_old_app(fake):
    m, _, _ = make(fake, None)
    with pytest.raises(BrowserError, match="обновите приложение"):
        BrowserSession(m, login="u")
    assert fake.sessions == {}


def test_bad_login_name_before_provisioning(fake, tmp_path):
    m, _, _ = make(fake, tmp_path)
    with pytest.raises(LoginError):
        BrowserSession(m, login="  ")
    assert fake.requests == []


def test_async_restore_and_save(fake, tmp_path):
    LoginStore(tmp_path).save("u", STATE)
    m, sink, ctxs = make(fake, tmp_path, AsyncContext)

    async def main():
        async with BrowserSession(m, login="u") as b:
            assert b.login_restored
            b.context.cookies.append({"name": "n2", "value": "v2", "domain": "x.ru", "path": "/"})

    asyncio.run(main())
    assert ctxs[0].storage == {"https://example.com": {"token": "ls-secret-456"}}
    assert [c["name"] for c in LoginStore(tmp_path).get("u").state["cookies"]] == ["sid", "n2"]
    assert fake.sessions == {}


def test_runtime_passes_logins_dir(script_file, tmp_path):
    path = script_file()
    params = tmp_path / "p.json"
    params.write_text('{"query": "q"}', "utf-8")
    env = {"DAWG_SCRIPT_PARAMS": str(params), "DAWG_SCRIPT_LOGINS_DIR": str(tmp_path / "L"),
           "DAWG_SCRIPT_EVENTS": str(tmp_path / "e.jsonl"), "DAWG_SCRIPT_OUTPUT_DIR": str(tmp_path / "o")}
    k = Kit.from_environment(script_path=path, argv=[], environ=env, cwd=tmp_path)
    assert k.browsers.logins_dir == tmp_path / "L"
    k.close()
    k2 = Kit.from_environment(script_path=path, argv=["--query", "q"], environ={}, cwd=tmp_path)
    assert k2.browsers.logins_dir == tmp_path / "logins"
    k2.close()
