"""Защита локального веб-интерфейса (ТЗ 6.4, 8, критерий 10.6).

Сервер слушает только 127.0.0.1. Каждый запрос проходит по порядку:

1. Host. Только `127.0.0.1:<порт>` и `localhost:<порт>`, иначе 403. Так страница
   чужого сайта не достучится до сервера через DNS rebinding: браузер пришлёт
   Host этого сайта.
2. Токен. Генерируется при старте и приходит один раз в ссылке `/?token=...`.
   Сервер кладёт его в cookie (HttpOnly, SameSite=Strict, имя с номером порта)
   и перенаправляет на ту же страницу без токена в адресе. Без верного cookie
   ответ 403. Токен сравнивается за постоянное время.
3. Метод. Разрешены GET, HEAD и POST. Всё, что меняет состояние, идёт POST.
4. POST: заголовок Origin (или Referer, если Origin нет) должен быть адресом
   этого сервера, а заголовок `X-CSRF-Token` равен CSRF-токену сервера. htmx
   ставит заголовок сам (static/app.js берёт токен из meta на странице).

Ответы получают заголовки: запрет встраивания во фреймы, CSP только со своих
адресов, `Referrer-Policy: no-referrer` (адрес с токеном не утекает) и
`Cache-Control: no-store`.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

CSRF_HEADER = "x-csrf-token"
ALLOWED_METHODS = ("GET", "HEAD", "POST")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


@dataclass
class Secrets:
    """Секреты одного запуска сервера. Живут только в памяти процесса."""

    port: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def cookie_name(self) -> str:
        # Cookie не различают порты: два сервера на одной машине не должны мешать друг другу.
        return f"dawg_replay_{self.port}"

    @property
    def hosts(self) -> tuple[str, ...]:
        return (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    @property
    def origins(self) -> tuple[str, ...]:
        return tuple(f"http://{h}" for h in self.hosts)

    def login_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"


def _same(a: str | None, b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode(), b.encode())


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, secrets_: Secrets):
        self.app = app
        self.s = secrets_

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)

        if headers.get("host") not in self.s.hosts:
            await _deny(send, 403, "Неверный адрес сервера. Откройте ссылку, которую показала команда dawg-replay ui.")
            return

        method = scope["method"]
        if method not in ALLOWED_METHODS:
            await _deny(send, 405, "Метод не поддерживается.")
            return

        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:
            pass
        morsel = cookie.get(self.s.cookie_name)
        has_cookie = _same(morsel.value if morsel else None, self.s.token)

        if method == "GET" and "token" in query:
            if not _same(query["token"], self.s.token):
                await _deny(send, 403, "Ссылка устарела. Откройте новую ссылку из команды dawg-replay ui.")
                return
            await _login(send, self.s, scope["path"], query)
            return

        if not has_cookie:
            await _deny(send, 403, "Нет доступа. Откройте ссылку, которую показала команда dawg-replay ui.")
            return

        if method == "POST":
            origin = headers.get("origin")
            if origin is None and headers.get("referer"):
                parts = urlsplit(headers["referer"])
                origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in self.s.origins:
                await _deny(send, 403, "Запрос пришёл не со страницы этого сервера.")
                return
            if not _same(headers.get(CSRF_HEADER), self.s.csrf):
                await _deny(send, 403, "Страница устарела. Обновите её и повторите действие.")
                return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + SECURITY_HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _login(send: Send, s: Secrets, path: str, query: dict[str, str]) -> None:
    rest = {k: v for k, v in query.items() if k != "token"}
    location = path + (("?" + urlencode(rest)) if rest else "")
    cookie = f"{s.cookie_name}={s.token}; Path=/; HttpOnly; SameSite=Strict"
    await send(
        {
            "type": "http.response.start",
            "status": 303,
            "headers": [(b"location", location.encode()), (b"set-cookie", cookie.encode()), *SECURITY_HEADERS],
        }
    )
    await send({"type": "http.response.body", "body": b""})


async def _deny(send: Send, status: int, text: str) -> None:
    body = (
        "<!doctype html><meta charset=utf-8><title>dawg-replay</title>"
        f"<p style='font:16px system-ui;margin:3rem'>{text}</p>"
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                *[h for h in SECURITY_HEADERS if h[0] != b"content-security-policy"],
                (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
