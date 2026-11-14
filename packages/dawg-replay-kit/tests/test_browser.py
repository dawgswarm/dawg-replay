import asyncio

import pytest
from fake_dawg import KEY, FakeDawg

from dawg_replay_kit.browser import BrowserLimitReached, Browsers, BrowserSession, Connection
from dawg_replay_kit.dawg_api import (
    LIMIT_MESSAGE,
    AsyncRestProvisioner,
    BrowserError,
    BrowserLimitError,
    RestProvisioner,
    Sessions,
    build_payload,
)
from dawg_replay_kit.events import Emitter


class ListSink:
    def __init__(self):
        self.events = []

    def write(self, e):
        self.events.append(e)

    def close(self):
        pass


@pytest.fixture
def fake():
    f = FakeDawg().start()
    yield f
    f.stop()


class FakeConn:
    def __init__(self):
        self.closed = False
        self.urls = []


def make(fake, key=KEY, **kw):
    sink = ListSink()
    events = Emitter(sink, secrets=[key] if key else [])
    conns = []

    def connect(ws_url):
        c = FakeConn()
        c.urls.append(ws_url)
        conns.append(c)
        return Connection(browser="B", context="C", page="P", close=lambda: setattr(c, "closed", True))

    async def aconnect(ws_url):
        c = FakeConn()
        conns.append(c)

        async def close():
            c.closed = True

        return Connection(browser="B", context="C", page="P", close=close)

    async def asleep(_):
        await asyncio.sleep(0)

    m = Browsers(
        events,
        key,
        fake.url,
        provisioner=lambda: RestProvisioner(key, fake.url, poll_interval=0.01, sleep=kw.get("sleep", lambda s: None)),
        async_provisioner=lambda: AsyncRestProvisioner(key, fake.url, poll_interval=0.01, sleep=asleep),
        sessions=lambda: Sessions(key, fake.url),
        connect=connect,
        aconnect=aconnect,
    )
    return m, sink, conns


def sessions_events(sink):
    return [(e["action"], e.get("session_id")) for e in sink.events if e["t"] == "session"]


def test_sync_happy_path(fake):
    m, sink, conns = make(fake)
    with BrowserSession(m, geo="moskva") as b:
        assert b.page == "P" and b.session_id in fake.sessions
        assert conns[0].urls[0].endswith("?token=eph-tok")
        assert "/dawg/baas/browser-1/devtools/browser/browser-1" in conns[0].urls[0]
        sid = b.session_id
    assert conns[0].closed
    assert fake.sessions == {}
    assert sessions_events(sink) == [("provisioning", None), ("open", sid), ("close", sid)]
    opened = next(e for e in sink.events if e.get("action") == "open")
    assert opened["browser_id"] == "browser-1"
    assert fake.requests[0] == ("POST", "/api/v1/browsers", {"geo": {"slug": "moskva"}})
    assert m.open_sessions() == {}


def test_open_written_before_ready_and_released_if_never_ready(fake):
    fake.ready_delay = 999
    m, sink, conns = make(fake)
    with pytest.raises(BrowserError, match="не готов"):
        with BrowserSession(m, ready_timeout=0.2):
            pass
    acts = sessions_events(sink)
    assert [a for a, _ in acts] == ["provisioning", "open", "close"]
    assert fake.count("GET", "/dawg/baas/") >= 1
    assert fake.sessions == {} and conns == []


def test_systemexit_during_wait_releases(fake, monkeypatch):
    m, sink, _ = make(fake)

    def boom(self, created, timeout):
        raise SystemExit(143)

    monkeypatch.setattr(RestProvisioner, "wait_ready", boom)
    with pytest.raises(SystemExit):
        with BrowserSession(m):
            pass
    assert [a for a, _ in sessions_events(sink)] == ["provisioning", "open", "close"]
    assert fake.sessions == {}


def test_exception_inside_with_releases(fake):
    m, sink, conns = make(fake)
    with pytest.raises(ZeroDivisionError):
        with BrowserSession(m):
            1 / 0
    assert fake.sessions == {} and conns[0].closed
    assert sessions_events(sink)[-1][0] == "close"


def test_limit_default_one(fake):
    m, _, _ = make(fake)
    with BrowserSession(m):
        with pytest.raises(BrowserLimitReached, match="kit.set_browser_limit"):
            with BrowserSession(m):
                pass
        assert len(fake.sessions) == 1
    with BrowserSession(m):  # слот вернулся
        pass


def test_limit_raised(fake):
    m, _, _ = make(fake)
    m.set_limit(2)
    with BrowserSession(m), BrowserSession(m):
        assert len(fake.sessions) == 2
    assert fake.sessions == {}
    with pytest.raises(ValueError):
        m.set_limit(0)


def test_429_retries_then_success(fake):
    fake.limit_429 = 2
    waits = []
    m, sink, _ = make(fake, sleep=waits.append)
    fake.retry_after = 7
    with BrowserSession(m):
        pass
    assert waits == [7, 7]
    warns = [e["msg"] for e in sink.events if e["t"] == "log"]
    assert len(warns) == 2 and "лимит тарифа" in warns[0]
    assert [a for a, _ in sessions_events(sink)] == ["provisioning", "open", "close"]


def test_429_exhausted(fake):
    fake.limit_429 = 100
    m, sink, _ = make(fake)
    with pytest.raises(BrowserLimitError, match=LIMIT_MESSAGE):
        with BrowserSession(m):
            pass
    assert fake.count("POST") == 4  # первый запрос и три повтора
    assert [a for a, _ in sessions_events(sink)] == ["provisioning", "failed"]
    fake.limit_429 = 0
    with BrowserSession(m):  # слот освобождён
        pass


@pytest.mark.parametrize("status, fragment", [(401, "ключ DAWG не подходит"), (500, "HTTP 500")])
def test_http_errors(fake, status, fragment):
    fake.post_status = status
    m, sink, _ = make(fake)
    with pytest.raises(BrowserError, match=fragment):
        with BrowserSession(m):
            pass
    assert [a for a, _ in sessions_events(sink)] == ["provisioning", "failed"]


def test_http_400_empty_body_hints_at_key(fake):
    """Заголовок с управляющим символом платформа отбрасывает кодом 400 без тела."""
    fake.post_status, fake.post_empty_body = 400, True
    m, sink, _ = make(fake)
    with pytest.raises(BrowserError, match="HTTP 400: запрос отклонён до проверки ключа") as e:
        with BrowserSession(m):
            pass
    assert "невидимых символов в ключе" in str(e.value)
    fake.post_empty_body = False
    with pytest.raises(BrowserError) as e:
        with BrowserSession(m):
            pass
    assert "невидимых" not in str(e.value)


def test_no_key(fake):
    m, sink, _ = make(fake, key=None)
    m._provisioner = lambda: RestProvisioner(m._key(), fake.url)
    with pytest.raises(BrowserError, match="DAWG_API_KEY"):
        with BrowserSession(m):
            pass
    assert fake.requests == []


def test_release_failure_keeps_session_open(fake):
    m, sink, _ = make(fake)
    fake.delete_fails = True
    with BrowserSession(m) as b:
        sid = b.session_id
    assert [a for a, _ in sessions_events(sink)] == ["provisioning", "open"]
    assert any("освободит приложение" in e.get("msg", "") for e in sink.events)
    assert m.open_sessions() == {sid: "browser-1"}
    fake.delete_fails = False
    m.close()  # повтор при выходе
    assert fake.sessions == {} and sessions_events(sink)[-1] == ("close", sid)


def test_close_releases_forgotten(fake):
    m, sink, _ = make(fake)
    b = BrowserSession(m).__enter__()
    m.close()
    assert fake.sessions == {}
    assert sessions_events(sink)[-1] == ("close", b.session_id)


def test_api_key_masked(fake):
    m, sink, _ = make(fake)
    m.events.log("info", f"ключ {KEY}")
    assert sink.events[0]["msg"] == "ключ ***"


def test_async(fake):
    m, sink, conns = make(fake)

    async def main():
        async with BrowserSession(m, proxy="socks5://u:p@h:1") as b:
            assert b.page == "P" and b.session_id in fake.sessions
            return b.session_id

    sid = asyncio.run(main())
    assert fake.sessions == {} and conns[0].closed
    assert sessions_events(sink) == [("provisioning", None), ("open", sid), ("close", sid)]
    assert fake.requests[0][2] == {"proxy": {"server": "socks5://h:1", "username": "u", "password": "p"}}


def test_async_cancel_during_wait(fake):
    fake.ready_delay = 999
    m, sink, _ = make(fake)

    async def main():
        task = asyncio.create_task(BrowserSession(m).__aenter__())
        while not any(e.get("action") == "open" for e in sink.events):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert [a for a, _ in sessions_events(sink)] == ["provisioning", "open", "close"]
    assert fake.sessions == {}


def test_async_cancel_during_wait_is_never_lost(fake):
    """Отмена во время запроса готовности не теряется (на 3.10 anyio внутри httpx мог её проглотить)."""
    from dawg_replay_kit.dawg_api import AsyncRestProvisioner

    fake.ready_delay = 999

    async def one(delay):
        prov = AsyncRestProvisioner(KEY, fake.url, poll_interval=0.0)
        try:
            created = await prov.create(build_payload(None, None))
            task = asyncio.create_task(prov.wait_ready(created, timeout=2))
            await asyncio.sleep(delay)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return "cancelled"
            except BrowserError:
                return "lost"
            return "returned"
        finally:
            await prov.close()

    async def main():
        return [await one((i % 20) * 0.0007) for i in range(100)]

    assert set(asyncio.run(main())) == {"cancelled"}


@pytest.mark.parametrize(
    "proxy, geo, expected",
    [
        (None, None, None),
        (None, "55.75, 37.61", {"geo": {"lat": 55.75, "lon": 37.61}}),
        (None, (1.5, 2.5), {"geo": {"lat": 1.5, "lon": 2.5}}),
        ("http://h:8080", None, {"proxy": {"server": "http://h:8080"}}),
    ],
)
def test_build_payload(proxy, geo, expected):
    assert build_payload(proxy, geo) == expected
