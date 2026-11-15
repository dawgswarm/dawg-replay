"""Локальный веб-сервер dawg-replay (ТЗ 6.4, 6.6).

Starlette + Jinja2 + htmx, всё лежит в пакете: без Node и CDN. Логики здесь нет,
она в ядре (`App`, `UiRuns`); представления в `views.py` только читают ядро и
рисуют шаблоны.

    serve(home)                 # dawg-replay ui
    create_app(home, secrets)   # приложение для тестов
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.secrets import Backend
from dawg_replay.ui_runs import UiRuns
from dawg_replay.web.security import Secrets, SecurityMiddleware

HERE = Path(__file__).parent
HOST = "127.0.0.1"


class State:
    """Общее состояние сервера: доступно в представлениях как request.app.state.ui."""

    def __init__(self, home: Path, secrets_: Secrets, keyring_backend: Backend | None = None):
        self.home = home
        self.secrets = secrets_
        self.keyring_backend = keyring_backend
        self.runs = UiRuns(home, app_factory=self.open_app)
        self.stopping = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None  # цикл сервера, ставится при старте
        self.telegram_pairing = None  # telegram.PairingJob, пока в Настройках ждём /start <код>

    def stop_soon(self) -> None:
        """Сообщить потокам SSE об остановке. Безопасно из обработчика сигнала и другого потока."""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.stopping.set)

    def open_app(self) -> App:
        """Ядро на один запрос или поток: у SQLite своё подключение на поток."""
        return App(self.home, keyring_backend=self.keyring_backend)


def create_app(
    home: Path,
    secrets_: Secrets,
    *,
    keyring_backend: Backend | None = None,
    log: Callable[[str], None] = lambda msg: None,
) -> Starlette:
    from dawg_replay.web import views

    state = State(home, secrets_, keyring_backend)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        state.loop = asyncio.get_running_loop()
        # Запуски, чьи супервизоры пропали (в том числе прошлый сервер, убитый жёстко).
        def recover() -> int:
            with state.open_app() as a:
                return len(launch.recover_lost(a))

        lost = await asyncio.to_thread(recover)
        if lost:
            log(f"потерянных запусков закрыто: {lost}")
        try:
            yield
        finally:
            state.stopping.set()
            if state.telegram_pairing is not None:
                state.telegram_pairing.cancel()
            active = state.runs.active()
            if active:
                log(f"останавливаю запуски ({len(active)}): мягкий сигнал, потом страховка браузеров")
            left = await asyncio.to_thread(state.runs.cancel_all)
            if left:
                log(f"не дождался запусков: {', '.join(left)}; их закроет recover_lost")

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    views.setup_templates(templates, secrets_)
    app = Starlette(
        routes=[*views.routes(templates), Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")],
        middleware=[Middleware(SecurityMiddleware, secrets_=secrets_)],
        lifespan=lifespan,
    )
    app.state.ui = state
    return app


def bind(port: int | None) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HOST, port or 0))
    sock.listen(128)
    sock.set_inheritable(False)
    return sock


def serve(
    home: Path,
    port: int | None = None,
    open_browser: bool = True,
    on_ready: Callable[[Secrets], None] | None = None,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr, flush=True),
) -> None:
    """Запускает сервер и работает до Ctrl+C или SIGTERM. Тогда запуски из UI отменяются по 5.7."""
    import uvicorn

    sock = bind(port)
    secrets_ = Secrets(port=sock.getsockname()[1])
    app = create_app(home, secrets_, log=log)
    config = uvicorn.Config(
        app,
        log_level="warning",
        lifespan="on",
        # Открытые SSE-соединения не должны держать остановку: запуски ждут отмены.
        timeout_graceful_shutdown=2,
    )
    server = make_server(config, app.state.ui)
    if on_ready is not None:
        on_ready(secrets_)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(secrets_.login_url(),)).start()
    server.run(sockets=[sock])


def _server_class():
    import uvicorn

    class Server(uvicorn.Server):
        """uvicorn ждёт закрытия соединений до lifespan shutdown, а потоки SSE узнают об
        остановке из state.stopping. Ставим его сразу по сигналу: открытые страницы
        отключаются сами, без traceback о превышенном timeout_graceful_shutdown."""

        def __init__(self, config: Any, state: State):
            super().__init__(config)
            self._ui_state = state

        def handle_exit(self, sig: int, frame: Any) -> None:
            self._ui_state.stop_soon()
            super().handle_exit(sig, frame)

    return Server


def make_server(config: Any, state: State) -> Any:
    return _server_class()(config, state)


async def sse_stream(
    request: Any, poll: Callable[[], Any], interval: float = 1.0, heartbeat: float = 15.0
) -> AsyncIterator[str]:
    """Общий помощник SSE: шлёт `event: change` с данными, когда poll() вернул новое значение.

    poll выполняется в потоке (ядро синхронное). Экран запуска подключит сюда RunView.follow().
    """
    state: State = request.app.state.ui
    last: Any = object()
    idle = 0.0
    yield "retry: 3000\n\n"
    while not state.stopping.is_set():
        if await request.is_disconnected():
            return
        value = await asyncio.to_thread(poll)
        if value != last:
            last = value
            idle = 0.0
            yield f"event: change\ndata: {value}\n\n"
        else:
            idle += interval
            if idle >= heartbeat:
                idle = 0.0
                yield ": ping\n\n"
        try:
            await asyncio.wait_for(state.stopping.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
