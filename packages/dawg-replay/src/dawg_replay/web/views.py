"""Представления веб-интерфейса. Только чтение ядра, вызов его методов и шаблоны.

Синхронные функции Starlette выполняет в пуле потоков; ядро открывается на
каждый запрос (`state.open_app()`), потому что у SQLite подключение на поток.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from dawg_replay.errors import ReplayError
from dawg_replay.runs import parse_ts
from dawg_replay.web.security import Secrets

# Статус запуска словами для человека и класс оформления.
STATUS = {
    "running": ("идёт", "running"),
    "ok": ("готово", "ok"),
    "partial": ("частично", "partial"),
    "failed": ("ошибка", "failed"),
    "failed (cancelled)": ("отменён", "muted"),
    "failed (timeout)": ("превышено время", "failed"),
    "failed (lost)": ("прерван", "muted"),
}
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def status_view(label: str | None) -> dict[str, str]:
    text, kind = STATUS.get(label or "", (label or "", "muted"))
    return {"text": text, "kind": kind}


def when(value: str | None, now: datetime | None = None) -> str:
    """Время для человека в местном поясе: «сегодня 20:44», «вчера 09:10», «13 ноя 20:44»."""
    dt = parse_ts(value) if value else None
    if dt is None:
        return ""
    local = dt.astimezone()
    now = (now or datetime.now(timezone.utc)).astimezone()
    hm = local.strftime("%H:%M")
    if local.date() == now.date():
        return f"сегодня {hm}"
    if local.date() == (now - timedelta(days=1)).date():
        return f"вчера {hm}"
    return f"{local.day} {MONTHS[local.month - 1]} {hm}"


def number(value: Any) -> str:
    """Число с разбивкой на разряды узким пробелом: 179 953."""
    try:
        return f"{float(value):,.0f}".replace(",", "\u202f")
    except (TypeError, ValueError):
        return ""


def setup_templates(templates: Jinja2Templates, secrets_: Secrets) -> None:
    env = templates.env
    env.globals["csrf_token"] = secrets_.csrf
    env.filters["when"] = when
    env.filters["status"] = status_view
    env.filters["number"] = number


def _ui(request: Request):
    return request.app.state.ui


def routes(templates: Jinja2Templates) -> list[Route]:
    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, {"active": ctx.pop("active", None), **ctx}, status_code=status_code)

    def fragment(request: Request, name: str, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx)

    # --- Скрипты ---

    def home(request: Request) -> Response:
        return RedirectResponse("/scripts", status_code=303)

    def scripts(request: Request) -> Response:
        with _ui(request).open_app() as app:
            items = app.scripts_overview()
        return page(request, "scripts.html", active="scripts", items=items)

    def scripts_list(request: Request) -> Response:
        with _ui(request).open_app() as app:
            items = app.scripts_overview()
        return fragment(request, "_scripts_list.html", items=items)

    def script_page(request: Request) -> Response:
        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            try:
                script = app.registry.get(name).to_dict()
            except ReplayError as e:
                return page(request, "error.html", active="scripts", message=e.message, status_code=404)
        return page(request, "script_stub.html", active="scripts", script=script)

    async def script_run(request: Request) -> Response:
        """Запуск из UI. Экран скрипта (следующий тикет) пришлёт сюда значения формы."""
        name = request.path_params["name"]
        form = await _form(request)
        sets = [f"{k}={v}" for k, v in form.items() if k != "preset" and v != ""]
        try:
            run = await run_in_threadpool(_ui(request).runs.start, name, preset=form.get("preset") or None, sets=sets)
        except ReplayError as e:
            return fragment(request, "_error.html", message=e.message)
        return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run.id}"})

    def run_page(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        with _ui(request).open_app() as app:
            try:
                run = app.runs.get(run_id).to_dict()
            except ReplayError as e:
                return page(request, "error.html", active="scripts", message=e.message, status_code=404)
        return page(request, "run_stub.html", active="scripts", run=run)

    async def runs_events(request: Request) -> Response:
        """SSE: сигнал «запуски изменились» для живого обновления списков."""
        from dawg_replay.web.server import sse_stream

        ui = _ui(request)

        def poll() -> str:
            with ui.open_app() as app:
                return app.runs_fingerprint()

        return StreamingResponse(
            sse_stream(request, poll),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # --- Настройки ---

    def settings(request: Request) -> Response:
        with _ui(request).open_app() as app:
            view = app.settings_view()
        return page(request, "settings.html", active="settings", s=view)

    async def settings_key(request: Request) -> Response:
        form = await _form(request)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    usage = app.set_api_key(form.get("api_key", ""), check=True)
                    resp = fragment(request, "_key.html", s=app.settings_view(), usage=usage, saved=True)
                    resp.headers["HX-Trigger"] = "key-saved"
                    return resp
                except ReplayError as e:
                    return fragment(request, "_key.html", s=app.settings_view(), error=e.message)

        return await run_in_threadpool(work)

    def settings_key_check(request: Request) -> Response:
        with _ui(request).open_app() as app:
            try:
                usage = app.check_api_key()
                return fragment(request, "_key.html", s=app.settings_view(), usage=usage, checked=True)
            except ReplayError as e:
                return fragment(request, "_key.html", s=app.settings_view(), error=e.message)

    def settings_usage(request: Request) -> Response:
        with _ui(request).open_app() as app:
            try:
                usage = app.check_api_key()
                return fragment(request, "_usage.html", usage=usage)
            except ReplayError as e:
                return fragment(request, "_usage.html", error=e.message, code=e.code)

    async def settings_limits(request: Request) -> Response:
        form = await _form(request)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.update_limits(form.get("max_parallel_runs", ""), form.get("max_timeout_minutes", ""))
                    return fragment(request, "_limits.html", s=app.settings_view(), saved=True)
                except ReplayError as e:
                    keys = ("max_parallel_runs", "max_timeout_minutes")
                    view = {**app.settings_view(), **{k: form.get(k, "") for k in keys}}
                    return fragment(request, "_limits.html", s=view, error=e.message)

        return await run_in_threadpool(work)

    return [
        Route("/", home),
        Route("/scripts", scripts),
        Route("/scripts/_list", scripts_list),
        Route("/scripts/{name}", script_page),
        Route("/scripts/{name}/run", script_run, methods=["POST"]),
        Route("/runs/{run_id}", run_page),
        Route("/events/runs", runs_events),
        Route("/settings", settings),
        Route("/settings/key", settings_key, methods=["POST"]),
        Route("/settings/key/check", settings_key_check, methods=["POST"]),
        Route("/settings/usage", settings_usage),
        Route("/settings/limits", settings_limits, methods=["POST"]),
    ]


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


__all__ = ["routes", "setup_templates", "status_view", "when"]
