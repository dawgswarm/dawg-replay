"""`kit.browser()`: облачный Chromium DAWG через Playwright по CDP (docs/contract.md, раздел 6).

    with kit.browser(geo="moskva") as b:
        b.page.goto("https://www.ozon.ru")

    async with kit.browser() as b:
        await b.page.goto("https://www.ozon.ru")

Порядок событий `session`:

- `provisioning` до запроса на создание;
- `open` с `session_id` сразу после ответа на POST, до ожидания готовности;
- `close` после освобождения, когда сессии больше нет в списке ключа;
- `failed`, если платформа ответила ошибкой и браузер точно не создан.

Браузер освобождается при выходе из `with`, при исключении, по сигналу отмены
(kit превращает его в SystemExit) и при выходе из процесса (atexit). Если процесс
убит жёстко, браузер освобождает приложение по событиям `open` без `close`.

Сколько браузеров одновременно держит скрипт, задаётся явно:
`kit.set_browser_limit(n)`, по умолчанию 1. Превышение даёт ошибку сразу,
без ожидания.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dawg_replay_kit.dawg_api import (
    READY_TIMEOUT,
    AsyncRestProvisioner,
    BrowserError,
    Created,
    RestProvisioner,
    Sessions,
    build_payload,
)
from dawg_replay_kit.events import Emitter

DEFAULT_LIMIT = 1


class BrowserLimitReached(BrowserError):
    """Скрипт пытается открыть больше браузеров, чем разрешил kit.set_browser_limit()."""


@dataclass
class Connection:
    browser: Any
    context: Any
    page: Any
    close: Callable[[], Any]  # для async возвращает корутину


# --- подключение Playwright -------------------------------------------------

_local = threading.local()


def _sync_connect(ws_url: str) -> Connection:
    """Подключение sync API. Один экземпляр Playwright на поток, пока открыт хоть один браузер."""
    from playwright.sync_api import sync_playwright

    if getattr(_local, "pw", None) is None:
        _local.pw = sync_playwright().start()
        _local.users = 0
    _local.users += 1
    try:
        browser = _local.pw.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
    except BaseException:
        _sync_release_pw()
        raise

    def close() -> None:
        try:
            browser.close()
        finally:
            _sync_release_pw()

    return Connection(browser, context, page, close)


def _sync_release_pw() -> None:
    _local.users -= 1
    if _local.users <= 0 and _local.pw is not None:
        pw, _local.pw = _local.pw, None
        try:
            pw.stop()
        except Exception:
            pass


_async_pw: dict[int, list[Any]] = {}  # id цикла -> [playwright, users]


async def _async_connect(ws_url: str) -> Connection:
    from playwright.async_api import async_playwright

    key = id(asyncio.get_running_loop())
    if key not in _async_pw:
        _async_pw[key] = [await async_playwright().start(), 0]
    entry = _async_pw[key]
    entry[1] += 1
    try:
        browser = await entry[0].chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
    except BaseException:
        await _async_release_pw(key)
        raise

    async def close() -> None:
        try:
            await browser.close()
        finally:
            await _async_release_pw(key)

    return Connection(browser, context, page, close)


async def _async_release_pw(key: int) -> None:
    entry = _async_pw.get(key)
    if entry is None:
        return
    entry[1] -= 1
    if entry[1] <= 0:
        _async_pw.pop(key, None)
        try:
            await entry[0].stop()
        except Exception:
            pass


# --- менеджер ---------------------------------------------------------------


class Browsers:
    """Учёт браузеров скрипта: лимит, события, освобождение."""

    def __init__(
        self,
        events: Emitter,
        api_key: str | None,
        base_url: str,
        *,
        provisioner: Callable[[], RestProvisioner] | None = None,
        async_provisioner: Callable[[], AsyncRestProvisioner] | None = None,
        sessions: Callable[[], Sessions] | None = None,
        connect: Callable[[str], Connection] = _sync_connect,
        aconnect: Callable[[str], Any] = _async_connect,
    ):
        self.events = events
        self._api_key = api_key
        self._base_url = base_url
        self._provisioner = provisioner or (lambda: RestProvisioner(self._key(), self._base_url))
        self._async_provisioner = async_provisioner or (lambda: AsyncRestProvisioner(self._key(), self._base_url))
        self._sessions_factory = sessions or (lambda: Sessions(self._key(), self._base_url))
        self._sessions: Sessions | None = None
        self.connect = connect
        self.aconnect = aconnect
        self._limit = DEFAULT_LIMIT
        self._in_use = 0
        self._open: dict[str, str] = {}  # session_id -> browser_id, ещё не освобождённые
        self._lock = threading.Lock()

    def _key(self) -> str:
        if not self._api_key:
            raise BrowserError("нет ключа DAWG: задайте переменную DAWG_API_KEY", created=False)
        return self._api_key

    @property
    def limit(self) -> int:
        return self._limit

    def set_limit(self, n: int) -> None:
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise ValueError("лимит браузеров должен быть целым не меньше 1")
        self._limit = n

    def open_sessions(self) -> dict[str, str]:
        with self._lock:
            return dict(self._open)

    # --- слот лимита ---

    def acquire(self) -> None:
        with self._lock:
            if self._in_use >= self._limit:
                raise BrowserLimitReached(
                    f"скрипт уже держит {self._in_use} из {self._limit} разрешённых браузеров; "
                    "закройте один или поднимите лимит kit.set_browser_limit(n)",
                    created=False,
                )
            self._in_use += 1

    def release_slot(self) -> None:
        with self._lock:
            self._in_use = max(0, self._in_use - 1)

    # --- события ---

    def on_retry(self, attempt: int, wait: float) -> None:
        self.events.log("warn", f"платформа отказала в браузере (лимит тарифа), повтор {attempt} через {wait:.0f} с")

    def provisioning(self) -> None:
        self.events.session("provisioning")

    def opened(self, created: Created) -> None:
        with self._lock:
            self._open[created.session_id] = created.browser_id
        self.events.session("open", created.session_id, created.browser_id)

    def failed(self, error: BrowserError) -> None:
        if error.created is False:
            self.events.session("failed")

    # --- освобождение ---

    def release(self, session_id: str) -> bool:
        """Освобождает сессию. `close` пишется, только если её больше нет в списке ключа."""
        with self._lock:
            if session_id not in self._open:
                return True
        try:
            if self._sessions is None:
                self._sessions = self._sessions_factory()
            gone = self._sessions.release(session_id)
        except Exception as e:
            self.events.log("error", f"браузер {session_id} не освобождён: {e}; его освободит приложение")
            return False
        if not gone:
            self.events.log("error", f"браузер {session_id} всё ещё в списке сессий; его освободит приложение")
            return False
        with self._lock:
            browser_id = self._open.pop(session_id, None)
        self.events.session("close", session_id, browser_id)
        return True

    def release_all(self) -> None:
        for sid in list(self.open_sessions()):
            self.release(sid)

    def close(self) -> None:
        self.release_all()
        if self._sessions is not None:
            try:
                self._sessions.close()
            except Exception:
                pass
            self._sessions = None


class BrowserSession:
    """Результат `kit.browser()`. Работает как `with` и как `async with`."""

    def __init__(
        self,
        manager: Browsers,
        proxy: str | None = None,
        geo: str | tuple[float, float] | None = None,
        ready_timeout: float = READY_TIMEOUT,
    ):
        self._m = manager
        self._payload = build_payload(proxy, geo)
        self._ready_timeout = ready_timeout
        self.session_id: str | None = None
        self.browser_id: str | None = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self._conn: Connection | None = None
        self._slot = False

    def __repr__(self) -> str:
        return f"<BrowserSession {self.session_id or 'not open'}>"

    # --- sync ---

    def __enter__(self) -> BrowserSession:
        self._m.acquire()
        self._slot = True
        try:
            prov = self._m._provisioner()
            try:
                self._m.provisioning()
                try:
                    created = prov.create(self._payload, self._m.on_retry)
                except BrowserError as e:
                    self._m.failed(e)
                    raise
                self._opened(created)
                ws_url = prov.wait_ready(created, self._ready_timeout)
            finally:
                prov.close()
            self._attach(self._m.connect(ws_url))
        except BaseException:
            self._cleanup_sync()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._cleanup_sync()

    def _cleanup_sync(self) -> None:
        try:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                try:
                    conn.close()
                except Exception:
                    pass
        finally:
            self._release_remote()

    # --- async ---

    async def __aenter__(self) -> BrowserSession:
        self._m.acquire()
        self._slot = True
        try:
            prov = self._m._async_provisioner()
            try:
                self._m.provisioning()
                try:
                    created = await prov.create(self._payload, self._m.on_retry)
                except BrowserError as e:
                    self._m.failed(e)
                    raise
                self._opened(created)
                ws_url = await prov.wait_ready(created, self._ready_timeout)
            finally:
                await prov.close()
            self._attach(await self._m.aconnect(ws_url))
        except BaseException:
            await self._cleanup_async()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._cleanup_async()

    async def _cleanup_async(self) -> None:
        try:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                try:
                    await conn.close()
                except Exception:
                    pass
        finally:
            # Освобождение короткое (DELETE и список), идёт синхронно: так оно
            # не прерывается отменой задачи на выходе из цикла.
            self._release_remote()

    # --- общее ---

    def _opened(self, created: Created) -> None:
        self.session_id, self.browser_id = created.session_id, created.browser_id
        self._m.opened(created)

    def _attach(self, conn: Connection) -> None:
        self._conn = conn
        self.browser, self.context, self.page = conn.browser, conn.context, conn.page

    def _release_remote(self) -> None:
        try:
            if self.session_id is not None:
                self._m.release(self.session_id)
        finally:
            if self._slot:
                self._slot = False
                self._m.release_slot()
