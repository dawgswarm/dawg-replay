"""Представления веб-интерфейса. Только чтение ядра, вызов его методов и шаблоны.

Синхронные функции Starlette выполняет в пуле потоков; ядро открывается на
каждый запрос (`state.open_app()`), потому что у SQLite подключение на поток.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from dawg_replay_kit.manifest import COLUMN_TYPE_WORDS

from dawg_replay import forms, imports, launch, minutes, repair, schedules, tg_rules, uistate, uploads
from dawg_replay.errors import ReplayError
from dawg_replay.presets import normalize as normalize_preset
from dawg_replay.presets import secret_tables_note
from dawg_replay.runs import parse_ts
from dawg_replay.uploads import Uploads
from dawg_replay.values import rows_word
from dawg_replay.web import home as home_data
from dawg_replay.web import projects_view, run_screen, telegram_view, transfer_data
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
    "waiting": ("ждёт слот", "muted"),
    "failed (skipped)": ("пропущен", "failed"),
    "failed (not_started)": ("не начат", "failed"),
}
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def status_view(label: str | None) -> dict[str, str]:
    text, kind = STATUS.get(label or "", (label or "", "muted"))
    return {"text": text, "kind": kind}


def run_status_view(run: dict[str, Any]) -> dict[str, str]:
    """Статус запуска для списков: тихая поломка (repair.annotate) вместо зелёного «готово»."""
    sus = run.get("suspicion")
    if sus:
        # Коротко: в списках бейдж стоит в колонке статусов, длинный текст ломает выравнивание.
        return {"text": "сайт поменялся?", "kind": "partial", "title": "Похоже, сайт поменялся. " + sus.get("text", "")}
    return {**status_view(run.get("label")), "title": ""}


def ru_moment(value: Any) -> str:
    """Дата, время или выражение для человека: 2026-11-01 -> 01.11.2026, 2026-11-01T09:30 -> 01.11.2026 09:30,
    today-1 -> вчера, 09:30 -> 09:30."""
    from dawg_replay_kit import relative

    if not value:
        return value
    text = str(value)
    if relative.is_relative(text, "datetime"):
        return relative.title(text)
    day, sep, t = text.partition("T")
    if len(day) == 10 and day[4] == "-" and day[7] == "-":
        return ".".join(reversed(day.split("-"))) + (f" {t}" if sep else "")
    return text


def ticks(text: str | None) -> str:
    """`имя` из текста ошибки в «имя»: в простом тексте обратные кавычки выглядят мусором."""
    return re.sub(r"`([^`\n]+)`", r"«\1»", text) if text else ""


HISTORY_CSV_LIMIT = 10000


def _local(value: str | None) -> str:
    """ISO UTC из БД в местное время для таблицы: «2026-11-15 18:43:07»."""
    dt = parse_ts(value) if value else None
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else ""


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


WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def when_next(value: datetime | str | None, now: datetime | None = None) -> str:
    """Будущее местное время: «сегодня 09:00», «завтра 09:00», «пн 16 ноя 09:00»."""
    if value is None:
        return ""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    now = now or datetime.now()
    hm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"сегодня {hm}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"завтра {hm}"
    return f"{WEEKDAYS_SHORT[dt.weekday()]} {dt.day} {MONTHS[dt.month - 1]} {hm}"


def number(value: Any) -> str:
    """Число с разбивкой на разряды узким пробелом: 179 953."""
    try:
        return f"{float(value):,.0f}".replace(",", "\u202f")
    except (TypeError, ValueError):
        return ""


def items_text(counts: dict[str, int] | None) -> str:
    """Итог отчёта по строкам для списков: «48 из 50, 2 упали, 1 пропущена»."""
    if not counts or not counts.get("total"):
        return ""
    parts = [f"{number(counts.get('ok', 0))} из {number(counts['total'])}"]
    if counts.get("failed"):
        parts.append(f"{number(counts['failed'])} {plural(counts['failed'], 'упала', 'упали', 'упали')}")
    if counts.get("skipped"):
        parts.append(f"{number(counts['skipped'])} {plural(counts['skipped'], 'пропущена', 'пропущены', 'пропущено')}")
    return ", ".join(parts)


_SPACES = re.compile(r"[\s\u00a0\u202f]+")


def run_items(run: dict[str, Any], summary_shown: bool = True) -> str:
    """Итог по строкам для строки списка. Пусто, если показан summary скрипта, а в нём уже
    есть то же «N из M»: «4 из 5, 1 упала» рядом с «4 из 5, 1 упала» не нужно."""
    counts = run.get("items")
    text = items_text(counts)
    if not text or not summary_shown or not run.get("summary"):
        return text
    summary = _SPACES.sub("", str(run["summary"]))
    return "" if f"{counts.get('ok', 0)}из{counts['total']}" in summary else text


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


def _versions() -> dict[str, str]:
    """Версии в подвале каждой страницы: какое приложение и kit сейчас запущены."""
    import dawg_replay
    import dawg_replay_kit
    from dawg_replay_kit.manifest import contract_versions

    return {"app": dawg_replay.__version__, "kit": dawg_replay_kit.__version__, "contract": ", ".join(contract_versions())}


def setup_templates(templates: Jinja2Templates, secrets_: Secrets) -> None:
    env = templates.env
    env.globals["csrf_token"] = secrets_.csrf
    env.globals["runs_active"] = runs_active
    env.globals["versions"] = _versions()
    env.filters["when"] = when
    env.filters["when_next"] = when_next
    env.filters["status"] = status_view
    env.filters["run_status"] = run_status_view
    env.filters["number"] = number
    env.tests["secret_column"] = forms.secret_column
    env.filters["plural"] = plural
    env.filters["items_text"] = items_text
    env.filters["run_items"] = run_items
    env.filters["minutes"] = minutes.fmt
    env.filters["rows_word"] = rows_word
    env.filters["type_words"] = lambda types: ", ".join(
        f"«{c}» {COLUMN_TYPE_WORDS.get(t, t)}" for c, t in (types or {}).items())
    env.filters["ticks"] = ticks
    env.filters["ru_date"] = ru_moment
    env.filters["choice_title"] = lambda value, fl: dict(fl.choice_titles or ()).get(value, value)
    env.filters["capitalize_first"] = lambda text: text[:1].upper() + text[1:] if text else text


def _ui(request: Request):
    return request.app.state.ui


def routes(templates: Jinja2Templates) -> list[Route]:
    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, {"active": ctx.pop("active", None), **ctx}, status_code=status_code)

    def fragment(request: Request, name: str, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx)

    # --- Главная ---

    def _dashboard(request: Request, visit: bool = False) -> dict[str, Any]:
        with _ui(request).open_app() as app:
            state = app.paths.home / "ui-state.json"
            since = uistate.visit(state) if visit else uistate.peek(state)
            return home_data.dashboard(app, run_screen.views_of(request), since)

    def home(request: Request) -> Response:
        return page(request, "home.html", active="home", d=_dashboard(request, visit=True))

    def home_live(request: Request) -> Response:
        return fragment(request, "home/_live.html", d=_dashboard(request))

    def home_usage(request: Request) -> Response:
        """Минуты и браузеры DAWG отдельным запросом: сеть не задерживает и не ломает главную."""
        with _ui(request).open_app() as app:
            week = minutes.period(app, days=7).to_dict()  # оценка по запускам, рядом точный остаток
            for s in week.get("scripts") or []:
                try:
                    s["title"] = app.registry.get(s["script"]).title
                except ReplayError:
                    s["title"] = s["script"]
            try:
                return fragment(request, "home/_usage.html", usage=app.check_api_key(), week=week)
            except ReplayError as e:
                return fragment(request, "home/_usage.html", error=e.message, code=e.code, week=week)

    # --- Скрипты ---

    def _library(request: Request) -> dict[str, Any]:
        q = request.query_params.get("q", "")[:200]
        site = request.query_params.get("site", "")[:200]
        builtin = request.query_params.get("builtin", "") == "1"
        with _ui(request).open_app() as app:
            return home_data.library(app, q, site, builtin)

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
                # Быстрый запуск формы не показывает: пресет, который новая версия принимает не целиком, не запускается.
                run = ui.runs.start(name, preset=preset, strict_preset=True)
            except ReplayError as e:
                what = f"«{preset}»" if preset else "без пресета"
                reason = e.message[:1].lower() + e.message[1:]
                return fragment(request, "_flash.html", message=f"Запуск {what} не начат: {reason}",
                                link=f"/scripts/{quote(name)}" + (f"?{urlencode({'preset': preset})}" if preset else "") + "#launch")
            return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run.id}"})

        return await run_in_threadpool(work)

    # --- Скрипт ---

    def _script_ctx(app, name: str, preset: str | None = None, sub=None, notice: str | None = None,
                    refs: dict[str, str] | None = None) -> dict[str, Any]:
        """Контекст формы скрипта: поля (из пресета или из отправленной формы), пресеты, ошибки.
        `refs`: файлы из результата другого запуска (кнопка «Передать в другой скрипт»)."""
        if sub is not None:
            form_ctx = {"fields": sub.fields, "preset": preset, "dropped": []}
            errors = sub.errors
        else:
            form_ctx = forms.build_fields(app, name, preset, refs=refs)
            errors = {}
        tg = tg_rules.connected(app)
        return {
            "name": name,
            "presets": [p.name for p in app.list_presets(name)],
            # Выбор «Сообщить, когда закончится»: только при подключённом Telegram
            "notify_hint": tg_rules.manual_text(tg_rules.effective(app, name)) if tg else None,
            "notify": None,
            "form_error": errors.get(forms.FORM_ERROR),
            "has_errors": bool(errors),
            "notice": notice,
            **form_ctx,
        }

    def script_page(request: Request) -> Response:
        name = request.path_params["name"]
        preset = request.query_params.get("preset") or None
        refs, passed = _passed_refs(request)
        with _ui(request).open_app() as app:
            try:
                view = app.script_view(name)
                ctx = _script_ctx(app, name, preset, refs=refs)
                if passed:
                    ctx["passed"] = transfer_data.passed_note(app, passed, refs)
                meta = home_data.manifest_meta(app, name)
                view["minutes"] = minutes.period(app, days=7, script=name).to_dict()
                repair.annotate(app, view["runs"])  # тихая поломка в истории скрипта
                view["projects"] = []
                for pname in app.projects.using(name):
                    try:
                        view["projects"].append({"name": pname, "title": app.projects.get(pname).title})
                    except ReplayError:
                        continue
            except ReplayError as e:
                return page(request, "error.html", active="scripts", message=e.message, status_code=404)
            lg = _logins_ctx(app, name)
            tg = _tg_script_ctx(app, name)
        run_screen.mark_questions(run_screen.views_of(request), view["runs"])
        view["cover"] = home_data.cover(view["script"]["title"], meta["site"])
        view["tags"] = list(meta["tags"])
        return page(request, "script.html", active="scripts", v=view, f=ctx, lg=lg, tg=tg)

    def _passed_refs(request: Request) -> tuple[dict[str, str], str | None]:
        """?from_run=<id>&use_<параметр>=<файл>: значения формы из файлов другого запуска."""
        run_id = (request.query_params.get("from_run") or "")[:64]
        if not run_id or not re.fullmatch(r"[0-9A-Za-z_-]+", run_id):
            return {}, None
        refs = {}
        for key, value in request.query_params.items():
            if key.startswith("use_") and value and len(value) <= 512:
                refs[key[4:]] = f"run:{run_id}:{value}"
        return refs, run_id

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
            runs = repair.annotate(app, [r.to_dict() for r in app.runs.list(name, 20)])
        run_screen.mark_questions(run_screen.views_of(request), runs)
        return fragment(request, "_script_runs.html", sname=name, runs=runs)

    def script_runs_csv(request: Request) -> Response:
        """История запусков скрипта в CSV для Excel: BOM, «;», местное время."""
        import csv
        import io

        from dawg_replay import repair as repair_mod

        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            try:
                app.registry.get(name)
            except ReplayError as e:
                return Response(e.message, status_code=404, media_type="text/plain; charset=utf-8")
            runs = [r.to_dict() for r in app.runs.list(name, HISTORY_CSV_LIMIT)]
            repair_mod.annotate_items(app, runs)
            counts = {r["id"]: repair_mod.scan(app.paths.run_dir(r["id"])).count for r in runs}
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(["Запуск", "Статус", "Начат", "Закончен", "Версия", "Пресет", "Как запущен", "Записей",
                    "Строк сделано", "Строк всего", "Итог", "Ошибка"])
        for r in runs:
            items = r.get("items") or {}
            w.writerow([
                r["id"], status_view(r["label"])["text"], _local(r["created_at"]), _local(r["finished_at"]),
                r["version"] or "", r["preset"] or "", "по расписанию" if r["trigger"] == "schedule" else "вручную",
                "" if counts.get(r["id"]) is None else counts[r["id"]],
                items.get("ok", "") if items else "", items.get("total", "") if items else "",
                r["summary"] or "", r["error"] or "",
            ])
        from urllib.parse import quote

        fname = f"{name}-запуски.csv"
        return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=\"runs.csv\"; filename*=UTF-8''{quote(fname)}"})

    async def script_run(request: Request) -> Response:
        """«Запустить»: форма разбирается forms.submit; при ошибках запуск не создаётся."""
        name = request.path_params["name"]
        form = await request.form()
        ui = _ui(request)
        preset = form.get("_preset") or None
        notify = form.get("_notify") if form.get("_notify") in tg_rules.RUN_CHOICES else None

        def work() -> Response:
            with ui.open_app() as app:
                try:
                    sub = forms.submit(app, name, form)
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message)
                if not sub.ok:
                    return fragment(request, "_script_form.html", f={**_script_ctx(app, name, preset, sub), "notify": notify})
                known = preset if preset in [p.name for p in app.list_presets(name)] else None
            try:
                run = ui.runs.start(name, preset=known, values=sub.values, notify=notify)
            except ReplayError as e:
                with ui.open_app() as app:
                    ctx = _script_ctx(app, name, preset, sub)
                ctx["form_error"] = e.message
                ctx["notify"] = notify
                return fragment(request, "_script_form.html", f=ctx)
            secret_tables = {k: t["id"] for k, t in sub.tables.items() if t.get("secret") and t.get("id")}
            if secret_tables:
                # Для «Перезапустить упавшие»: секретной таблицы нет в истории, запуск помнит загрузку.
                with ui.open_app() as app:
                    Uploads(app.paths.uploads).link(run.id, secret_tables)
            return Response(status_code=204, headers={"HX-Redirect": f"/runs/{run.id}"})

        return await run_in_threadpool(work)

    async def script_save_preset(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        preset_name = normalize_preset(form.get("_preset_name"))

        def work() -> Response:
            with _ui(request).open_app() as app:
                if not preset_name:
                    sub = forms.parse(app.registry.manifest(name), form, require=False, uploads=Uploads(app.paths.uploads))
                    sub.fields = forms.fields_for(app.registry.manifest(name), raw=sub.raw, secret_status=app.secret_status(name),
                                                  tables=sub.tables,
                                                  sources=forms.result_sources(app, app.registry.manifest(name), sub.values))
                    ctx = _script_ctx(app, name, None, sub)
                    ctx["form_error"] = "Введите имя пресета."
                    return fragment(request, "_script_form.html", f=ctx)
                sub = forms.save_preset(app, name, preset_name, form)
                notice = None
                if sub.ok:
                    note = secret_tables_note(app.registry.manifest(name), app.get_preset(name, preset_name))
                    notice = f"Пресет «{preset_name}» сохранён." + (f" {note}" if note else "")
                resp = fragment(request, "_script_form.html", f=_script_ctx(app, name, preset_name if sub.ok else None, sub, notice))
                if sub.ok:
                    resp.headers["HX-Trigger"] = "presets-changed"  # блок «Регулярный запуск» видит новый пресет
                return resp

        return await run_in_threadpool(work)

    async def script_table_upload(request: Request) -> Response:
        """Файл в поле таблицы: разбор и проверка сразу, в ответ поле с превью или ошибкой."""
        name, param = request.path_params["name"], request.path_params["param"]
        data, filename, problem = b"", "", None
        limit = f"Файл больше {uploads.MAX_BYTES // 1_000_000} МБ, разбейте таблицу на части"
        try:
            length = int(request.headers.get("content-length", ""))
        except ValueError:
            length, problem = 0, "Браузер не сообщил размер файла, выберите файл заново"
        if problem is None and length > uploads.MAX_BYTES + 32 * 1024:
            problem = limit
        if problem is None:
            try:
                form = await request.form(max_files=1, max_fields=4)
            except (MultiPartException, HTTPException):
                problem = "Загрузите один файл таблицы: .csv, .tsv, .xlsx или .json"
            else:
                try:
                    files = [v for v in form.getlist("_table_file") if isinstance(v, UploadFile)]
                    if len(files) != 1:
                        problem = "Загрузите один файл таблицы: .csv, .tsv, .xlsx или .json"
                    else:
                        data = await files[0].read(uploads.MAX_BYTES + 1)
                        filename = files[0].filename or ""
                        if len(data) > uploads.MAX_BYTES:
                            problem, data = limit, b""
                finally:
                    await form.close()

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.registry.get(name)
                    out = forms.upload_table(app, name, param, data, filename, problem=problem)
                except ReplayError as e:
                    return Response(e.message, status_code=404, media_type="text/plain; charset=utf-8")
            return fragment(request, "_table_field.html", fl=out["field"], script=name)

        return await run_in_threadpool(work)

    async def script_file_upload(request: Request) -> Response:
        """Файлы в поле параметра file (контракт 1.3): проверка и сохранение, в ответ поле с именами."""
        name, param = request.path_params["name"], request.path_params["param"]

        def limits() -> tuple[int, int]:
            with _ui(request).open_app() as app:
                try:
                    p = app.registry.manifest(name).param(param)
                except (ReplayError, KeyError):
                    return 0, 0
            if p.type != "file":
                return 0, 0
            return p.size_limit, (p.max_items or 50) if p.multiple else 1

        per_file, count = await run_in_threadpool(limits)
        files: list[tuple[str, bytes]] = []
        problem = None
        if not per_file:
            return Response("Такого поля файла нет.", status_code=404, media_type="text/plain; charset=utf-8")
        try:
            length = int(request.headers.get("content-length", ""))
        except ValueError:
            length, problem = 0, "Браузер не сообщил размер файла, выберите файл заново"
        if problem is None and length > per_file * count + 64 * 1024 * count:
            problem = f"файлы больше {per_file // 1024 // 1024} МБ каждый" if count > 1 else f"файл больше {per_file // 1024 // 1024} МБ"
        if problem is None:
            try:
                form = await request.form(max_files=count, max_fields=4)
            except (MultiPartException, HTTPException):
                problem = "здесь нужен один файл" if count == 1 else f"файлов не больше {count}"
            else:
                try:
                    for v in form.getlist("_file_upload"):
                        if isinstance(v, UploadFile):
                            data = await v.read(per_file + 1)
                            files.append((v.filename or "файл", data))
                finally:
                    await form.close()

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    out = forms.upload_file(app, name, param, files, problem=problem)
                except ReplayError as e:
                    return Response(e.message, status_code=404, media_type="text/plain; charset=utf-8")
            return fragment(request, "_table_field.html", fl=out["field"], script=name)

        return await run_in_threadpool(work)

    async def script_table_sheet(request: Request) -> Response:
        """Другой лист загруженного XLSX (скрипт не объявил `sheet`, листов несколько)."""
        name, param = request.path_params["name"], request.path_params["param"]
        form = await _form(request)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.registry.get(name)
                    out = forms.table_sheet(app, name, param, form.get("_upload", ""), form.get("_sheet", ""))
                except ReplayError as e:
                    return Response(e.message, status_code=404, media_type="text/plain; charset=utf-8")
            return fragment(request, "_table_field.html", fl=out["field"], script=name)

        return await run_in_threadpool(work)

    async def script_table_manual(request: Request) -> Response:
        """«Ввести вручную»: строки из формы становятся таблицей, как загруженный файл."""
        name, param = request.path_params["name"], request.path_params["param"]
        form = await request.form(max_files=0, max_fields=forms.MANUAL_MAX_COLUMNS * 10_001 + 10)
        columns = [str(v) for v in form.getlist("_cols")]
        cells = [str(v) for v in form.getlist("_cells")]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.registry.get(name)
                    out = forms.manual_table(app, name, param, columns, cells)
                except ReplayError as e:
                    return Response(e.message, status_code=404, media_type="text/plain; charset=utf-8")
            return fragment(request, "_table_field.html", fl=out["field"], script=name)

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

    # --- Регулярный запуск ---

    kinds = [("daily", "каждый день"), ("weekly", "раз в неделю"), ("hours", "каждые несколько часов"), ("cron", "выражение cron")]
    weekday_titles = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

    def _schedules_ctx(app, name: str, error: str | None = None, notice: str | None = None,
                       form: dict[str, str] | None = None) -> dict[str, Any]:
        app.registry.get(name)
        data = app.schedules.list(name)
        for item in data["schedules"]:
            item["next_text"] = when_next(item["next_run"])
            try:
                item["outdated"] = app.get_preset(name, item["preset"]).outdated()
            except ReplayError:
                item["outdated"] = None
        cron_ok = app.schedules.backend.kind == "cron"
        presets = [p.name for p in app.list_presets(name)]
        values = {"preset": presets[0] if presets else "", "kind": "daily", "time": "09:00", "weekday": "1", "hours": "6", "cron": ""}
        values.update({k: v for k, v in (form or {}).items() if v})
        tg = tg_rules.connected(app)
        for item in data["schedules"]:
            item["tg"] = tg_rules.form_ctx(app, "schedule", item["id"], name) if tg else None
        return {
            "name": name, "entries": data["schedules"], "scheduler_error": data["error"], "error": error, "notice": notice,
            "presets": presets, "form": values, "kinds": kinds if cron_ok else kinds[:3], "cron_supported": cron_ok,
            "weekdays": weekday_titles, "every_hours": schedules.EVERY_HOURS,
        }

    def _spec(form: dict[str, str]) -> schedules.Spec:
        kind = form.get("kind")
        if kind == "daily":
            return schedules.Spec.daily(form.get("time", ""))
        if kind == "weekly":
            return schedules.Spec.weekly(form.get("weekday", ""), form.get("time", ""))
        if kind == "hours":
            return schedules.Spec.every_hours(form.get("hours", ""))
        if kind == "cron":
            return schedules.Spec.from_cron(form.get("cron", ""))
        raise ReplayError("bad_schedule", "выберите, как часто запускать")

    def script_schedules(request: Request) -> Response:
        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            try:
                ctx = _schedules_ctx(app, name)
            except ReplayError as e:
                return fragment(request, "_error.html", message=e.message)
        return fragment(request, "_schedules.html", sc=ctx)

    async def script_schedule_add(request: Request) -> Response:
        name = request.path_params["name"]
        raw = await _form(request)
        form = {k: str(raw.get(f"_{k}", "") or "").strip()[:200] for k in ("preset", "kind", "time", "weekday", "hours", "cron")}

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.registry.get(name)
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message[:1].upper() + e.message[1:])
                try:
                    s = app.schedules.add(name, form["preset"], _spec(form))
                except ReplayError as e:
                    msg = e.message[:1].upper() + e.message[1:]
                    return fragment(request, "_schedules.html", sc=_schedules_ctx(app, name, error=msg, form=form))
                notice = f"Расписание добавлено: {s.spec.describe()}, пресет «{s.preset}»."
                return fragment(request, "_schedules.html", sc=_schedules_ctx(app, name, notice=notice))

        return await run_in_threadpool(work)

    async def script_schedule_delete(request: Request) -> Response:
        name, sid = request.path_params["name"], request.path_params["sid"]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.registry.get(name)
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message[:1].upper() + e.message[1:])
                try:
                    if app.schedules.get(sid).script != name:
                        raise ReplayError("schedule_not_found", "это расписание другого скрипта")
                    app.schedules.remove(sid)
                except ReplayError as e:
                    msg = e.message[:1].upper() + e.message[1:]
                    return fragment(request, "_schedules.html", sc=_schedules_ctx(app, name, error=msg))
                return fragment(request, "_schedules.html", sc=_schedules_ctx(app, name, notice="Регулярный запуск снят."))

        return await run_in_threadpool(work)

    # --- Уведомления в Telegram: правила ---

    def _tg_script_ctx(app: Any, name: str, **extra: Any) -> dict[str, Any]:
        connected = tg_rules.connected(app)
        return {"connected": connected, "tr": tg_rules.form_ctx(app, "script", name, **extra) if connected else None}

    def _tg_saved(rows: dict[str, Any]) -> str:
        return "Сохранено." + ("" if rows else " Всё как в общих настройках.")

    async def settings_tg_rules(request: Request) -> Response:
        form = await _form(request)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    tg_rules.update_general(app, tg_rules.parse_form("general", form))
                    tr = tg_rules.form_ctx(app, "general", saved="Сохранено.")
                except ReplayError as e:
                    tr = tg_rules.form_ctx(app, "general", error=e.message[:1].upper() + e.message[1:])
                return fragment(request, "_telegram_rules.html", tr=tr)

        return await run_in_threadpool(work)

    async def script_tg_rules(request: Request) -> Response:
        name = request.path_params["name"]
        form = await _form(request)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.registry.get(name)
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message[:1].upper() + e.message[1:])
                try:
                    own = tg_rules.set_overrides(app, "script", name, tg_rules.parse_form("script", form))
                    tr = tg_rules.form_ctx(app, "script", name, saved=_tg_saved(own))
                except ReplayError as e:
                    tr = tg_rules.form_ctx(app, "script", name, error=e.message[:1].upper() + e.message[1:])
                return fragment(request, "_telegram_rules.html", tr=tr)

        return await run_in_threadpool(work)

    async def schedule_tg_rules(request: Request) -> Response:
        name, sid = request.path_params["name"], request.path_params["sid"]
        form = await _form(request)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    if app.schedules.get(sid).script != name:
                        raise ReplayError("schedule_not_found", "это расписание другого скрипта")
                except ReplayError as e:
                    return fragment(request, "_error.html", message=e.message[:1].upper() + e.message[1:])
                try:
                    own = tg_rules.set_overrides(app, "schedule", sid, tg_rules.parse_form("schedule", form))
                    saved = "Сохранено." + ("" if own else " Всё как у скрипта.")
                    tr = tg_rules.form_ctx(app, "schedule", sid, name, saved=saved)
                except ReplayError as e:
                    tr = tg_rules.form_ctx(app, "schedule", sid, name, error=e.message[:1].upper() + e.message[1:])
                return fragment(request, "_telegram_rules.html", tr=tr)

        return await run_in_threadpool(work)

    # --- Сохранённые входы ---

    def _logins_ctx(app: Any, name: str, notice: str | None = None, error: str | None = None) -> dict[str, Any]:
        try:
            items = app.list_logins(name)
        except (ReplayError, OSError):
            items = []
        return {"name": name, "items": items, "notice": notice, "error": error, "open": bool(notice or error)}

    async def script_logins_forget(request: Request) -> Response:
        """«Забыть» один вход (_login) или все (_all=1). Отвечает блоком входов."""
        name = request.path_params["name"]
        form = await _form(request)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    if form.get("_all"):
                        n = app.forget_logins(name)
                        notice = f"Стёрто входов: {n}. Скрипт войдёт заново при следующем запуске."
                    else:
                        login = str(form.get("_login", ""))
                        app.forget_login(name, login)
                        notice = f"Вход «{login}» стёрт. Скрипт войдёт заново при следующем запуске."
                except ReplayError as e:
                    msg = e.message[:1].upper() + e.message[1:]
                    return fragment(request, "_logins.html", lg=_logins_ctx(app, name, error=msg))
                return fragment(request, "_logins.html", lg=_logins_ctx(app, name, notice=notice))

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
            tg = telegram_view.view(app, getattr(_ui(request), "telegram_pairing", None))
        return page(request, "settings.html", active="settings", s=view, tg=tg)

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
                    app.update_limits(form.get("max_parallel_runs", ""), form.get("max_timeout_minutes", ""),
                                      form.get("rows_preview_limit"), form.get("log_limit"))
                    request.app.state.ui.run_views = None  # новые лимиты действуют на открываемые запуски
                    return fragment(request, "_limits.html", s=app.settings_view(), saved=True)
                except ReplayError as e:
                    keys = ("max_parallel_runs", "max_timeout_minutes", "rows_preview_limit", "log_limit")
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
                    if getattr(rec, "collected", False):
                        raise ReplayError("collected_run", "это сборка шага проекта, её не запускают: перезапустите проект с упавшего")
                    presets = [p.name for p in app.list_presets(rec.script_name)]
                    values = launch.again_values(app, rec)
                except ReplayError as e:
                    link = f"/scripts/{quote(rec.script_name)}#launch" if e.code == "needs_upload" else None
                    return fragment(request, "_flash.html", message=f"Запуск не начат: {e.message}" if link else e.message, link=link)
            try:
                run = ui.runs.start(rec.script_name, preset=rec.preset if rec.preset in presets else None, values=values,
                                    inputs_of=rec.id)
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
        Route("/scripts/{name}/runs.csv", script_runs_csv),
        Route("/scripts/{name}/run", script_run, methods=["POST"]),
        Route("/scripts/{name}/presets", script_save_preset, methods=["POST"]),
        Route("/scripts/{name}/_table/{param}", script_table_upload, methods=["POST"]),
        Route("/scripts/{name}/_file/{param}", script_file_upload, methods=["POST"]),
        Route("/scripts/{name}/_table/{param}/manual", script_table_manual, methods=["POST"]),
        Route("/scripts/{name}/_table/{param}/sheet", script_table_sheet, methods=["POST"]),
        Route("/scripts/{name}/rollback", script_rollback, methods=["POST"]),
        Route("/scripts/{name}/_schedules", script_schedules),
        Route("/scripts/{name}/schedules", script_schedule_add, methods=["POST"]),
        Route("/scripts/{name}/schedules/{sid}/delete", script_schedule_delete, methods=["POST"]),
        Route("/scripts/{name}/schedules/{sid}/telegram", schedule_tg_rules, methods=["POST"]),
        Route("/scripts/{name}/telegram", script_tg_rules, methods=["POST"]),
        Route("/scripts/{name}/logins/forget", script_logins_forget, methods=["POST"]),
        *run_screen.routes(templates),
        *projects_view.routes(templates),
        Route("/events/runs", runs_events),
        Route("/events/active", runs_active_count),
        Route("/settings", settings),
        Route("/settings/key", settings_key, methods=["POST"]),
        Route("/settings/key/check", settings_key_check, methods=["POST"]),
        Route("/settings/usage", settings_usage),
        Route("/settings/limits", settings_limits, methods=["POST"]),
        *telegram_view.routes(templates, _form),
        Route("/settings/telegram/rules", settings_tg_rules, methods=["POST"]),
    ]


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


__all__ = ["routes", "setup_templates", "status_view", "when"]
