"""Работа с браузерами DAWG по REST (docs/contract.md, раздел 6).

Создание браузера изолировано в `RestProvisioner` и `AsyncRestProvisioner`:
POST `/api/v1/browsers`, колбэк с id сессии сразу после ответа, затем ожидание
готовности. Если в `dawg-sdk-python` появится колбэк после POST, заменяется
только этот слой.

Освобождение идёт через `release(session_id)` из `dawg-sdk-python`. SDK глотает
ошибки DELETE, поэтому после него сессия проверяется по `list_sessions`.

Модуль используют и kit (в скрипте), и приложение (страховка после запуска).
Зависимости (`httpx`, `dawg-sdk-python`) импортируются при первом вызове.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE_URL = "https://dawgswarm.ru"
HTTP_TIMEOUT = 30.0
READY_TIMEOUT = 120.0
POLL_INTERVAL = 1.0
LIMIT_RETRIES = 3
MAX_RETRY_WAIT = 60.0
ALIVE_STATUSES = ("ACTIVE", "IDLE")

LIMIT_MESSAGE = "достигнут лимит одновременных браузеров тарифа"


class BrowserError(RuntimeError):
    """Браузер DAWG не получен."""

    def __init__(self, message: str, created: bool | None = None):
        super().__init__(message)
        # False: сервер ответил ошибкой, браузер точно не создан.
        # None: неизвестно (обрыв связи после отправки запроса).
        self.created = created


class BrowserLimitError(BrowserError):
    """Платформа не дала браузер из-за лимита тарифа, повторы не помогли."""


@dataclass(frozen=True)
class Created:
    session_id: str
    browser_id: str
    ephemeral_token: str | None


def build_payload(proxy: str | None, geo: str | tuple[float, float] | None) -> dict[str, Any] | None:
    """Тело POST в том же виде, что делает dawg-sdk-python."""
    payload: dict[str, Any] = {}
    if proxy:
        server = {"server": proxy}
        if "@" in proxy and "://" in proxy:
            proto_auth, host_port = proxy.rsplit("@", 1)
            proto, auth = proto_auth.split("://", 1)
            if ":" in auth:
                user, password = auth.split(":", 1)
                server = {"server": f"{proto}://{host_port}", "username": user, "password": password}
        payload["proxy"] = server
    if geo:
        if isinstance(geo, str) and geo.count(",") == 1:
            try:
                lat, lon = (float(x.strip()) for x in geo.split(","))
                geo = (lat, lon)
            except ValueError:
                pass
        payload["geo"] = {"slug": geo} if isinstance(geo, str) else {"lat": geo[0], "lon": geo[1]}
    return payload or None


def _retry_after(resp: Any) -> float:
    try:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and "retry_after_seconds" in detail:
            return float(detail["retry_after_seconds"])
    except Exception:
        pass
    try:
        return float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return 60.0


def _parse_created(resp: Any) -> Created:
    body = resp.json()
    data = body.get("data", body) if isinstance(body, dict) else {}
    sid = data.get("sessionId") or data.get("session_id")
    bid = data.get("browserId") or data.get("browser_id")
    if not sid or not bid:
        # Ответ успешный, но без id: браузер, возможно, создан. Страховка увидит provisioning без open.
        raise BrowserError("платформа создала браузер, но не вернула его id", created=None)
    return Created(str(sid), str(bid), data.get("ephemeralToken") or data.get("ephemeral_token"))


def _http_error(resp: Any) -> BrowserError:
    if resp.status_code == 401:
        return BrowserError("ключ DAWG не подходит (401)", created=False)
    text = ""
    try:
        text = resp.text[:200]
    except Exception:
        pass
    return BrowserError(f"платформа не создала браузер: HTTP {resp.status_code} {text}".rstrip(), created=False)


def _ws_url(base_url: str, created: Created, debugger_url: str, api_key: str) -> str:
    path = debugger_url.split("/devtools/", 1)[1]
    ws_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    auth = f"token={created.ephemeral_token}" if created.ephemeral_token else f"apiKey={api_key}"
    return f"{ws_base}/dawg/baas/{created.browser_id}/devtools/{path}?{auth}"


def _ready_url(base_url: str, created: Created) -> str:
    return f"{base_url}/dawg/baas/{created.browser_id}/json/version"


OnRetry = Callable[[int, float], None]


class RestProvisioner:
    """Синхронное создание браузера."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        limit_retries: int = LIMIT_RETRIES,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        import httpx

        self._httpx = httpx
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._limit_retries = limit_retries
        self._poll = poll_interval
        self._sleep = sleep
        self._client = httpx.Client(headers={"X-API-Key": api_key}, timeout=HTTP_TIMEOUT)

    def close(self) -> None:
        self._client.close()

    def create(self, payload: dict[str, Any] | None, on_retry: OnRetry | None = None) -> Created:
        """POST с повторами при лимите тарифа. Возвращается сразу после ответа."""
        attempt = 0
        while True:
            try:
                resp = self._client.post(f"{self.base_url}/api/v1/browsers", json=payload)
            except self._httpx.ConnectError as e:
                raise BrowserError(f"нет связи с платформой DAWG: {e}", created=False) from None
            except self._httpx.HTTPError as e:
                raise BrowserError(f"обрыв связи при создании браузера: {e}", created=None) from None
            if resp.status_code == 429:
                attempt += 1
                if attempt > self._limit_retries:
                    raise BrowserLimitError(LIMIT_MESSAGE, created=False)
                wait = min(_retry_after(resp), MAX_RETRY_WAIT)
                if on_retry:
                    on_retry(attempt, wait)
                self._sleep(wait)
                continue
            if resp.status_code >= 400:
                raise _http_error(resp)
            return _parse_created(resp)

    def wait_ready(self, created: Created, timeout: float = READY_TIMEOUT) -> str:
        deadline = time.monotonic() + timeout
        url = _ready_url(self.base_url, created)
        while True:
            try:
                resp = self._client.get(url)
                if resp.status_code == 200:
                    dbg = resp.json().get("webSocketDebuggerUrl", "")
                    if "/devtools/" in dbg:
                        return _ws_url(self.base_url, created, dbg, self.api_key)
                elif resp.status_code not in (502, 503, 504, 404):
                    raise BrowserError(f"браузер не стартовал: HTTP {resp.status_code}")
            except self._httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise BrowserError(f"браузер не готов за {timeout:.0f} с")
            self._sleep(self._poll)


class AsyncRestProvisioner:
    """То же для asyncio."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        limit_retries: int = LIMIT_RETRIES,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        import httpx

        self._httpx = httpx
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._limit_retries = limit_retries
        self._poll = poll_interval
        self._sleep = sleep
        self._client = httpx.AsyncClient(headers={"X-API-Key": api_key}, timeout=HTTP_TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()

    async def create(self, payload: dict[str, Any] | None, on_retry: OnRetry | None = None) -> Created:
        attempt = 0
        while True:
            try:
                resp = await self._client.post(f"{self.base_url}/api/v1/browsers", json=payload)
            except self._httpx.ConnectError as e:
                raise BrowserError(f"нет связи с платформой DAWG: {e}", created=False) from None
            except self._httpx.HTTPError as e:
                raise BrowserError(f"обрыв связи при создании браузера: {e}", created=None) from None
            if resp.status_code == 429:
                attempt += 1
                if attempt > self._limit_retries:
                    raise BrowserLimitError(LIMIT_MESSAGE, created=False)
                wait = min(_retry_after(resp), MAX_RETRY_WAIT)
                if on_retry:
                    on_retry(attempt, wait)
                await self._sleep(wait)
                continue
            if resp.status_code >= 400:
                raise _http_error(resp)
            return _parse_created(resp)

    async def wait_ready(self, created: Created, timeout: float = READY_TIMEOUT) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        url = _ready_url(self.base_url, created)
        while True:
            try:
                resp = await self._client.get(url)
                if resp.status_code == 200:
                    dbg = resp.json().get("webSocketDebuggerUrl", "")
                    if "/devtools/" in dbg:
                        return _ws_url(self.base_url, created, dbg, self.api_key)
                elif resp.status_code not in (502, 503, 504, 404):
                    raise BrowserError(f"браузер не стартовал: HTTP {resp.status_code}")
            except self._httpx.HTTPError:
                pass
            if loop.time() >= deadline:
                raise BrowserError(f"браузер не готов за {timeout:.0f} с")
            await self._sleep(self._poll)


# --- освобождение и список сессий через dawg-sdk-python ---


class Sessions:
    """Сессии ключа: список и освобождение по id. Синхронно, годится и для atexit."""

    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL):
        from dawg_sdk import Baas

        self._baas = Baas(api_key, base_url.rstrip("/"))

    def list(self) -> list[dict[str, Any]]:
        return self._baas.list_sessions()

    def alive_ids(self) -> set[str]:
        return {str(s.get("id")) for s in self.list() if str(s.get("status", "ACTIVE")).upper() in ALIVE_STATUSES}

    def release(self, session_id: str) -> bool:
        """Освобождает сессию и проверяет по списку, что её нет. True, если сессии больше нет."""
        self._baas.release(session_id)
        try:
            return session_id not in self.alive_ids()
        except Exception:
            return False

    def close(self) -> None:
        self._baas.close()

