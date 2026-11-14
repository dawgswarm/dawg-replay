"""Представления веб-интерфейса. Только чтение ядра, вызов его методов и шаблоны.

Синхронные функции Starlette выполняет в пуле потоков; ядро открывается на
каждый запрос (`state.open_app()`), потому что у SQLite подключение на поток.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from dawg_replay import forms, imports, minutes
from dawg_replay.errors import ReplayError
from dawg_replay.runs import parse_ts
from dawg_replay.web import home as home_data
from dawg_replay.web import run_screen
from dawg_replay.web.security import Secrets

# Тело запроса загрузки: файл и поля multipart. Меньше порога SpooledTemporaryFile Starlette
# (1 МиБ), поэтому загруженный файл не попадает во временный каталог системы.
UPLOAD_BODY_MAX = imports.MAX_BYTES + 32 * 1024

ICON = Path(__file__).parent / "static" / "icon.svg"

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


def plural(n: Any, one: str, few: str, many: str) -> str:
    """Форма слова для числа: 1 сообщение, 2 сообщения, 5 сообщений."""
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def runs_active(request: Request) -> bool:
    """Есть ли идущие запуски: от этого зависит анимация знака в шапке."""
    try:
        with _ui(request).open_app() as app:
            return app.running_count() > 0
    except (AttributeError, ReplayError, sqlite3.Error, OSError):
        return False


def setup_templates(templates: Jinja2Templates, secrets_: Secrets) -> None:
    env = templates.env
    env.globals["csrf_token"] = secrets_.csrf
    env.globals["runs_active"] = runs_active
    env.filters["when"] = when
    env.filters["status"] = status_view
    env.filters["number"] = number
    env.filters["plural"] = plural
    env.filters["minutes"] = minutes.fmt
    env.filters["capitalize_first"] = lambda text: text[:1].upper() + text[1:] if text else text


def _ui(request: Request):
    return request.app.state.ui


def routes(templates: Jinja2Templates) -> list[Route]:
    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, {"active": ctx.pop("active", None), **ctx}, status_code=status_code)

    def fragment(request: Request, name: str, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx)

    # --- Главная ---

    def _dashboard(request: Request) -> dict[str, Any]:
        with _ui(request).open_app() as app:
            return home_data.dashboard(app, run_screen.views_of(request))

    def home(request: Request) -> Response:
        return page(request, "home.html", active="home", d=_dashboard(request))

    def home_live(request: Request) -> Response:
        return fragment(request, "home/_live.html", d=_dashboard(request))

    def home_usage(request: Request) -> Response:
        """Минуты и браузеры DAWG отдельным запросом: сеть не задерживает и не ломает главную."""
        with _ui(request).open_app() as app:
            week = minutes.period(app, days=7).to_dict()  # оценка по запускам, рядом точный остаток
            try:
                return fragment(request, "home/_usage.html", usage=app.check_api_key(), week=week)
            except ReplayError as e:
                return fragment(request, "home/_usage.html", error=e.message, code=e.code, week=week)

    # --- Скрипты ---

    def _library(request: Request) -> dict[str, Any]:
        q = request.query_params.get("q", "")[:200]
        site = request.query_params.get("site", "")[:200]
        with _ui(request).open_app() as app:
            return home_data.library(app, q, site)

    def scripts(request: Request) -> Response:
        lib = _library(request)
        removed = request.query_params.get("removed", "")[:64]
        return page(request, "scripts.html", active="scripts", lib=lib, search_q=lib["query"], removed=removed)

    def scripts_list(request: Request) -> Response:
        return fragment(request, "_scripts_list.html", lib=_library(request))

    async def script_quick(request: Request) -> Response:
        """Быстрый запуск по пресету с карточки или главной."""
        name = request.path_params["name"]
        form = await request.form()
        preset = (form.get("_preset") or "").strip() or None
        ui = _ui(request)

        def work() -> Response:
            try:
                run = ui.runs.start(name, preset=preset)
            except ReplayError as e:
                what = f"«{preset}»" if preset else "без пресета"
                return fragment(request, "_flash.html", message=f"Запуск {what} не начат: {e.message}",
                                link=f"/scripts/{quote(name)}" + (f"?{urlencode({'preset': preset})}" if preset else "") + "#launch")
            return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run.id}"})

        return await run_in_threadpool(work)

    # --- Скрипт ---

    def _script_ctx(app, name: str, preset: str | None = None, sub=None, notice: str | None = None) -> dict[str, Any]:
        """Контекст формы скрипта: поля (из пресета или из отправленной формы), пресеты, ошибки."""
        if sub is not None:
            form_ctx = {"fields": sub.fields, "preset": preset, "dropped": []}
            errors = sub.errors
        else:
            form_ctx = forms.build_fields(app, name, preset)
            errors = {}
        return {
            "name": name,
            "presets": [p.name for p in app.list_presets(name)],
            "form_error": errors.get(forms.FORM_ERROR),
            "has_errors": bool(errors),
            "notice": notice,
            **form_ctx,
        }

    def script_page(request: Request) -> Response:
        name = request.path_params["name"]
        preset = request.query_params.get("preset") or None
        with _ui(request).open_app() as app:
            try:
                view = app.script_view(name)
                ctx = _script_ctx(app, name, preset)
                meta = home_data.manifest_meta(app, name)
                view["minutes"] = minutes.period(app, days=7, script=name).to_dict()
            except ReplayError as e:
                return page(request, "error.html", active="scripts", message=e.message, status_code=404)
        view["cover"] = home_data.cover(view["script"]["title"], meta["site"])
        view["tags"] = list(meta["tags"])
        return page(request, "script.html", active="scripts", v=view, f=ctx)

    def script_form(request: Request) -> Response:
        """Форма заново: выбор пресета подставляет его значения."""
        name = request.path_params["name"]
        preset = request.query_params.get("_preset") or None
        with _ui(request).open_app() as app:
            try:
                ctx = _script_ctx(app, name, preset)
            except ReplayError as e:
                ctx = {**_script_ctx(app, name), "form_error": e.message}
        return fragment(request, "_script_form.html", f=ctx)

    def script_runs(request: Request) -> Response:
        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            runs = [r.to_dict() for r in app.runs.list(name, 20)]
        return fragment(request, "_script_runs.html", sname=name, runs=runs)

    async def script_run(request: Request) -> Response:
        """«Запустить»: форма разбирается forms.submit; при ошибках запуск не создаётся."""
        name = request.path_params["name"]
        form = await request.form()
        ui = _ui(request)
        preset = form.get("_preset") or None

        def work() -> Response:
            with ui.open_app() as app:
                try:
                    sub = forms.submit(app, name, form)
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message)
                if not sub.ok:
                    return fragment(request, "_script_form.html", f=_script_ctx(app, name, preset, sub))
                known = preset if preset in [p.name for p in app.list_presets(name)] else None
            try:
                run = ui.runs.start(name, preset=known, values=sub.values)
            except ReplayError as e:
                with ui.open_app() as app:
                    ctx = _script_ctx(app, name, preset, sub)
                ctx["form_error"] = e.message
                return fragment(request, "_script_form.html", f=ctx)
            return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run.id}"})

        return await run_in_threadpool(work)

    async def script_save_preset(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        preset_name = (form.get("_preset_name") or "").strip()

        def work() -> Response:
            with _ui(request).open_app() as app:
                if not preset_name:
                    sub = forms.parse(app.registry.manifest(name), form, require=False)
                    sub.fields = forms.fields_for(app.registry.manifest(name), raw=sub.raw, secret_status=app.secret_status(name))
                    ctx = _script_ctx(app, name, None, sub)
                    ctx["form_error"] = "Введите имя пресета."
                    return fragment(request, "_script_form.html", f=ctx)
                sub = forms.save_preset(app, name, preset_name, form)
                notice = f"Пресет «{preset_name}» сохранён." if sub.ok else None
                return fragment(request, "_script_form.html", f=_script_ctx(app, name, preset_name if sub.ok else None, sub, notice))

        return await run_in_threadpool(work)

    async def script_rollback(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.registry.rollback(name, int(form.get("_version", "")))
                except (ReplayError, ValueError) as e:
                    msg = e.message if isinstance(e, ReplayError) else "неверный номер версии"
                    return fragment(request, "_error.html", message=msg)
            return Response(status_code=204, headers={"HX-Redirect": f"/scripts/{name}"})

        return await run_in_threadpool(work)

    # --- Импорт и удаление скрипта ---

    def import_page(request: Request) -> Response:
        return page(request, "import.html", active="scripts", max_mb=imports.MAX_BYTES // 1_000_000)

    async def import_upload(request: Request) -> Response:
        """Шаг 1: файл проверяется как в add; при успехе переход к карточке."""

        def fail(message: str, issues: Any = None) -> Response:
            return fragment(request, "_import_errors.html", message=message, issues=issues)

        try:
            length = int(request.headers.get("content-length", ""))
        except ValueError:
            return fail("Браузер не сообщил размер файла. Выберите файл заново.")
        if length > UPLOAD_BODY_MAX:
            return fail("Файл больше 1 МБ, это не похоже на скрипт dawg-replay.")
        try:
            form = await request.form(max_files=1, max_fields=4, max_part_size=64 * 1024)
        except (MultiPartException, HTTPException):  # лишние файлы и поля, битый multipart
            return fail("Загрузите один файл .py.")
        try:
            files = [v for v in form.getlist("file") if isinstance(v, UploadFile)]
            if len(files) != 1:
                return fail("Загрузите один файл .py.")
            data = await files[0].read(imports.MAX_BYTES + 1)
            filename = files[0].filename or ""
        finally:
            await form.close()

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    import_id = imports.Imports(app).stage(data, filename)
                except ReplayError as e:
                    if e.code == "invalid_manifest":
                        first = e.message.splitlines()[0]
                        return fail(first, e.details)
                    return fail(e.message[:1].upper() + e.message[1:])
            return Response(status_code=204, headers={"HX-Redirect": f"/scripts/_import/{import_id}"})

        return await run_in_threadpool(work)

    def import_card(request: Request) -> Response:
        """Шаг 2: что будет добавлено и чем отличается от текущей версии."""
        import_id = request.path_params["import_id"]
        with _ui(request).open_app() as app:
            try:
                card = imports.Imports(app).preview(import_id)
            except ReplayError as e:
                return page(request, "import.html", active="scripts", max_mb=imports.MAX_BYTES // 1_000_000,
                            error=e.message[:1].upper() + e.message[1:], issues=e.details if e.code == "invalid_manifest" else None,
                            status_code=200 if e.code != "import_not_found" else 404)
        m = card["manifest"]
        card["cover"] = home_data.cover(m["title"], card["site"])
        return page(request, "import_card.html", active="scripts", c=card, m=m)

    async def import_add(request: Request) -> Response:
        """Шаг 3: регистрация и переход на страницу скрипта."""
        import_id = request.path_params["import_id"]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    result = imports.Imports(app).commit(import_id)
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message[:1].upper() + e.message[1:])
            return Response(status_code=204, headers={"HX-Redirect": f"/scripts/{result.script.name}"})

        return await run_in_threadpool(work)

    async def script_delete(request: Request) -> Response:
        """Удаление как `dawg-replay remove --yes`; подтверждение вводом name скрипта."""
        name = request.path_params["name"]
        form = await _form(request)

        def work() -> Response:
            if form.get("_confirm", "").strip() != name:
                return fragment(request, "_error.html", message=f"Для удаления введите имя скрипта: {name}")
            with _ui(request).open_app() as app:
                try:
                    app.remove(name)
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message[:1].upper() + e.message[1:])
            return Response(status_code=204, headers={"HX-Redirect": f"/scripts?{urlencode({'removed': name})}"})

        return await run_in_threadpool(work)

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

    def runs_active_count(request: Request) -> Response:
        """Число идущих запусков для страниц без потока /events/runs (анимация знака в шапке)."""
        from starlette.responses import JSONResponse

        with _ui(request).open_app() as app:
            n = app.running_count()
        return JSONResponse({"running": n})

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

    async def run_again(request: Request) -> Response:
        """«Запустить снова»: те же значения параметров (без секретов, они берутся из хранилища)."""
        run_id = request.path_params["run_id"]
        ui = _ui(request)

        def work() -> Response:
            with ui.open_app() as app:
                try:
                    rec = app.runs.get(run_id)
                    if rec.draft:
                        raise ReplayError("draft_run", "черновой запуск повторяется только из терминала")
                    names = {p.name for p in app.registry.manifest(rec.script_name).params}
                    presets = [p.name for p in app.list_presets(rec.script_name)]
                except ReplayError as e:
                    return fragment(request, "_flash.html", message=e.message, link=None)
            values = {k: v for k, v in rec.params.items() if k in names}
            try:
                run = ui.runs.start(rec.script_name, preset=rec.preset if rec.preset in presets else None, values=values)
            except ReplayError as e:
                return fragment(request, "_flash.html", message=f"Запуск не начат: {e.message}", link=f"/scripts/{quote(rec.script_name)}#launch")
            return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run.id}"})

        return await run_in_threadpool(work)

    def favicon(request: Request) -> Response:
        """Браузеры запрашивают /favicon.ico сами; отдаём тот же SVG, что в <link rel="icon">."""
        return FileResponse(ICON, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})

    return [
        Route("/favicon.ico", favicon),
        Route("/", home),
        Route("/home/_live", home_live),
        Route("/home/usage", home_usage),
        Route("/scripts", scripts),
        Route("/scripts/{name}/quick", script_quick, methods=["POST"]),
        Route("/runs/{run_id}/again", run_again, methods=["POST"]),
        Route("/scripts/_list", scripts_list),
        Route("/scripts/_import", import_page),
        Route("/scripts/_import", import_upload, methods=["POST"]),
        Route("/scripts/_import/{import_id}", import_card),
        Route("/scripts/_import/{import_id}/add", import_add, methods=["POST"]),
        Route("/scripts/{name}/delete", script_delete, methods=["POST"]),
        Route("/scripts/{name}", script_page),
        Route("/scripts/{name}/_form", script_form),
        Route("/scripts/{name}/_runs", script_runs),
        Route("/scripts/{name}/run", script_run, methods=["POST"]),
        Route("/scripts/{name}/presets", script_save_preset, methods=["POST"]),
        Route("/scripts/{name}/rollback", script_rollback, methods=["POST"]),
        *run_screen.routes(templates),
        Route("/events/runs", runs_events),
        Route("/events/active", runs_active_count),
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
