"""Блок Telegram в Настройках: токен бота, привязка чата по коду, проверочное сообщение, отключение."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from dawg_replay import telegram, tg_rules
from dawg_replay.errors import ReplayError


def _job(request: Request) -> telegram.PairingJob | None:
    return getattr(request.app.state.ui, "telegram_pairing", None)


def view(app: Any, job: telegram.PairingJob | None) -> dict[str, Any]:
    """Данные блока: состояние подключения и, если идёт или только что закончилась, привязка."""
    v = telegram.status(app)
    if job is not None:
        until = datetime.fromtimestamp(job.until, timezone.utc).astimezone()
        v["pairing"] = {**job.pairing.to_dict(), "state": job.state, "error": job.error,
                        "until": until.strftime("%H:%M"), "chat_name": (job.result or {}).get("chat_name")}
    else:
        v["pairing"] = None
    # Правила «о чём писать» (_telegram_rules.html), только при привязанном чате
    v["rules"] = tg_rules.form_ctx(app, "general") if v["connected"] else None
    return v


def routes(templates: Jinja2Templates, form_reader: Any) -> list[Route]:
    def fragment(request: Request, **ctx: Any) -> Response:
        with request.app.state.ui.open_app() as app:
            tg = view(app, _job(request))
        return templates.TemplateResponse(request, "_telegram.html", {"tg": tg, **ctx})

    def panel(request: Request) -> Response:
        """Блок целиком; пока ждём /start, его опрашивает сам блок."""
        job = _job(request)
        resp = fragment(request)
        if job is not None and job.state != "waiting":
            request.app.state.ui.telegram_pairing = None  # итог показан один раз
        return resp

    async def connect(request: Request) -> Response:
        form = await form_reader(request)
        token = form.get("token")

        def work() -> Response:
            ui = request.app.state.ui
            old = _job(request)
            if old is not None:
                old.cancel(wait=telegram.POLL_EXTRA)  # два getUpdates сразу Telegram не разрешает
                ui.telegram_pairing = None
            with ui.open_app() as app:
                try:
                    pairing = telegram.start_pairing(app, token or None)
                except ReplayError as e:
                    return fragment(request, error=e.message)
            ui.telegram_pairing = telegram.PairingJob(ui.open_app, pairing).start()
            return fragment(request)

        return await run_in_threadpool(work)

    def cancel(request: Request) -> Response:
        job = _job(request)
        if job is not None:
            job.cancel()
            request.app.state.ui.telegram_pairing = None
        return fragment(request)

    def send_test(request: Request) -> Response:
        with request.app.state.ui.open_app() as app:
            try:
                telegram.send_test(app)
            except ReplayError as e:
                return fragment(request, error=e.message)
        return fragment(request, tested=True)

    def disconnect(request: Request) -> Response:
        job = _job(request)
        if job is not None:
            job.cancel()
            request.app.state.ui.telegram_pairing = None
        with request.app.state.ui.open_app() as app:
            try:
                telegram.disconnect(app)
            except ReplayError as e:
                return fragment(request, error=e.message)
        return fragment(request, disconnected=True)

    return [
        Route("/settings/telegram", panel),
        Route("/settings/telegram/connect", connect, methods=["POST"]),
        Route("/settings/telegram/cancel", cancel, methods=["POST"]),
        Route("/settings/telegram/test", send_test, methods=["POST"]),
        Route("/settings/telegram/disconnect", disconnect, methods=["POST"]),
    ]
