"""Экран «Запуск» (ТЗ 5.4, 6.4 п.3): состояние запуска из events.jsonl и живое обновление.

Состояние даёт `RunView` (dawg_replay.runview), один на запуск на весь сервер:
так номера изменений (`seq`) одинаковы для страницы и для потока SSE, и клиент
после переподключения догоняет с `Last-Event-ID`.

Поток `/runs/{id}/events` (SSE):

- `id: <seq>` + `event: patch`, в data JSON `{"panels": {id: html}, "log": [html, ...]}`.
  Панели рисует сервер теми же шаблонами, что и страницу (Jinja экранирует всё),
  клиент только заменяет их и дописывает строки журнала.
- `event: resync`: клиент отстал дальше хранимой истории, сервер перезапускался
  или файл событий подменили: страница перезагружается.
- `event: done`: запуск завершён и всё дочитано, клиент закрывает поток.

Работает и для запусков, начатых из CLI: всё берётся из каталога запуска и БД.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from dawg_replay import diagnosis, insurance, launch, minutes
from dawg_replay.errors import ReplayError
from dawg_replay.runs import parse_ts
from dawg_replay.runview import RunView

VIEWS_CACHE = 32  # сколько запусков держать открытыми одновременно
SSE_INTERVAL = 0.5
SSE_HEARTBEAT = 15.0
DONE_AFTER = 2.0  # после завершения запуска ещё столько ждём строки страховки

# Какие панели перерисовать на изменение данного вида.
PANELS_BY_KIND = {
    "stage": ("trail", "result"),
    "progress": ("progress",),
    "metric": ("metrics",),
    "rows": ("tables",),
    "artifact": ("files",),
    "browsers": ("head",),
    "result": ("result",),
    "run": ("head", "result", "suspects", "files", "trail"),
}
PANELS = ("head", "result", "suspects", "trail", "progress", "metrics", "files", "tables")

REASONS = {
    "cancelled": "Запуск отменили. Собранное до отмены осталось в файлах результатов, если скрипт успел их сохранить.",
    "timeout": "Запуск шёл дольше разрешённого и был остановлен.",
    "lost": "Запуск прервался: программа, которая его вела, закрылась или компьютер выключился.",
    "start_error": "Скрипт не удалось запустить.",
}

LEVEL_WORDS = {"debug": "отладка", "info": "инфо", "warn": "внимание", "error": "ошибка", "raw": "сырая строка"}


# --- общий вид запуска на сервер ------------------------------------------------


class Views:
    """Кеш RunView по id запуска. Один вид на запуск, чтобы seq совпадал у страницы и SSE."""

    def __init__(self, state: Any, size: int = VIEWS_CACHE):
        self._state = state
        self._size = size
        self._lock = threading.Lock()
        self._views: OrderedDict[str, RunView] = OrderedDict()

    def get(self, run_id: str) -> RunView:
        with self._lock:
            view = self._views.get(run_id)
            if view is not None:
                self._views.move_to_end(run_id)
                return view
        view = self._make(run_id)
        with self._lock:
            view = self._views.setdefault(run_id, view)
            self._views.move_to_end(run_id)
            while len(self._views) > self._size:
                self._views.popitem(last=False)
        return view

    def _make(self, run_id: str) -> RunView:
        state = self._state
        with state.open_app() as app:
            rec = app.runs.get(run_id)  # ReplayError для неизвестного id
            rows_limit = app.config.rows_preview_limit
            run_dir = app.paths.run_dir(run_id)
            try:
                title = app.registry.version(rec.script_name, rec.script_version).title if not rec.draft else rec.script_name
            except ReplayError:
                title = rec.script_name

        def run_info() -> dict[str, Any]:
            # Своё подключение к БД на вызов: RunView опрашивают из разных потоков.
            with state.open_app() as a:
                rec_ = a.runs.get(run_id)
                r = rec_.to_dict()
                dg = diagnosis.diagnose(a, rec_)
            keep = ("id", "script", "version", "draft", "status", "reason", "label", "created_at", "started_at",
                    "finished_at", "exit_code", "summary", "error", "killed", "cancel_requested", "trigger", "schedule_id")
            info = {k: r.get(k) for k in keep}
            info["title"] = title
            info["suspects"] = insurance.pending_suspects(run_dir)
            info["diagnosis"] = dg  # упал, ничего не сообщив: хвост stderr и подсказка
            # Примерные минуты браузеров DAWG: только для завершённого, у идущего число растёт само.
            info["minutes"] = None if rec_.running else minutes.for_run(run_dir, rec_).to_dict()
            return info

        return RunView(run_dir, rows_limit=rows_limit, run_info=run_info)


def views_of(request: Request) -> Views:
    state = request.app.state.ui
    v = getattr(state, "run_views", None)
    if v is None:
        v = Views(state)
        state.run_views = v
    return v


# --- безопасный markdown для summary --------------------------------------------

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
_LIST_MARK = re.compile(r"^\s*[-*]\s+")


def _inline(text: str) -> str:
    """Строка уже экранирована. Ссылки только http(s), открываются в новой вкладке."""
    codes: list[str] = []

    def keep_code(m: re.Match[str]) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE.sub(keep_code, text)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2)}" rel="noopener noreferrer" target="_blank">{m.group(1)}</a>', text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], text)


def markdown(text: str | None) -> str:
    """Короткий markdown итога: абзацы, списки «- », **жирный**, *курсив*, `код`, ссылки.
    Сырой HTML не проходит: всё экранируется до разметки."""
    if not text:
        return ""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", html.escape(text.strip(), quote=True)):
        lines = block.split("\n")
        if all(re.match(r"\s*[-*]\s+", ln) for ln in lines):
            items = "".join("<li>" + _inline(_LIST_MARK.sub("", ln)) + "</li>" for ln in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append("<p>" + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    return "".join(out)


# --- фильтры шаблонов -----------------------------------------------------------


def clock(ts: str | None) -> str:
    dt = parse_ts(ts) if ts else None
    return dt.astimezone().strftime("%H:%M:%S") if dt else ""


def value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):,}".replace(",", " ")
        return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".")
    return "" if value is None else str(value)


def cell(value: Any) -> str:
    """Значение ячейки таблицы. Числа без разбивки на разряды: в таблицах часто идентификаторы (sku, id)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def size_text(n: int | None) -> str:
    if n is None:
        return ""
    for unit in ("байт", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n} {unit}" if unit == "байт" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return ""


def setup(templates: Jinja2Templates) -> None:
    env = templates.env
    env.filters["clock"] = clock
    env.filters["value"] = value_text
    env.filters["cell"] = cell
    env.tests["number"] = is_number
    env.filters["size"] = size_text
    env.filters["markdown"] = markdown
    env.globals["REASONS"] = REASONS
    env.globals["LEVEL_WORDS"] = LEVEL_WORDS


def context(snapshot: dict[str, Any]) -> dict[str, Any]:
    run = snapshot.get("run") or {}
    return {"s": snapshot, "run": run, "running": run.get("status") == "running"}


def render_panels(templates: Jinja2Templates, snapshot: dict[str, Any], names: tuple[str, ...] | set[str]) -> dict[str, str]:
    ctx = context(snapshot)
    return {name: templates.get_template(f"run/_{name}.html").render(ctx) for name in PANELS if name in names}


def render_log(templates: Jinja2Templates, entries: list[dict[str, Any]]) -> list[str]:
    tpl = templates.get_template("run/_log_entry.html")
    return [tpl.render(e=e) for e in entries]


def patch_for(templates: Jinja2Templates, view: RunView, changes: list[dict[str, Any]]) -> dict[str, Any]:
    names: set[str] = set()
    entries: list[dict[str, Any]] = []
    for c in changes:
        names.update(PANELS_BY_KIND.get(c["kind"], ()))
        if c["kind"] == "log":
            entries.append(c["data"])
    snap = view.snapshot() if names else {}
    return {"panels": render_panels(templates, snap, names) if names else {}, "log": render_log(templates, entries)}


# --- файлы результатов ----------------------------------------------------------


def safe_output_file(output_dir: Path, rel: str) -> Path | None:
    """Файл внутри каталога output или None. Путь проверяется заново: без выхода наружу,
    в том числе через символические ссылки."""
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return None
    try:
        out = output_dir.resolve(strict=True)
        full = (out / rel).resolve(strict=True)
        full.relative_to(out)
    except (OSError, ValueError, RuntimeError):
        return None
    return full if full.is_file() else None


def open_folder(path: Path) -> None:
    """Открывает папку в файловом менеджере ОС. Без shell."""
    if sys.platform == "darwin":
        argv = ["open", str(path)]
    elif sys.platform == "win32":
        argv = ["explorer", str(path)]
    else:
        argv = ["xdg-open", str(path)]
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- SSE ------------------------------------------------------------------------


def _event(name: str, data: Any, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _after(request: Request) -> int:
    for raw in (request.headers.get("last-event-id"), request.query_params.get("after")):
        if raw is not None:
            try:
                return max(0, int(raw))
            except ValueError:
                return -1  # не число: пусть клиент возьмёт страницу заново
    return 0


async def run_stream(request: Request, templates: Jinja2Templates, view: RunView, after: int,
                     interval: float = SSE_INTERVAL, heartbeat: float = SSE_HEARTBEAT, done_after: float = DONE_AFTER):
    stopping: asyncio.Event = request.app.state.ui.stopping
    yield "retry: 3000\n\n"
    seq = after
    idle = 0.0
    finished_idle = 0.0
    while not stopping.is_set():
        if await request.is_disconnected():
            return
        batch = await asyncio.to_thread(view.changes, seq)
        if seq < 0 or batch["resync"] or seq > batch["seq"]:
            yield _event("resync", {"seq": batch["seq"]})
            return
        if batch["changes"]:
            patch = await asyncio.to_thread(patch_for, templates, view, batch["changes"])
            seq = batch["seq"]
            idle = 0.0
            finished_idle = 0.0
            yield _event("patch", patch, seq)
        else:
            idle += interval
            if view.finished:
                finished_idle += interval
                if finished_idle >= done_after:
                    yield _event("done", {"seq": seq}, seq)
                    return
            if idle >= heartbeat:
                idle = 0.0
                yield ": ping\n\n"
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# --- маршруты -------------------------------------------------------------------


def routes(templates: Jinja2Templates) -> list[Route]:
    setup(templates)

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, {"active": "scripts", **ctx}, status_code=status_code)

    def notice(request: Request, text: str, ok: bool = True, status_code: int = 200) -> Response:
        return templates.TemplateResponse(request, "run/_notice.html", {"text": text, "ok": ok}, status_code=status_code)

    def get_view(request: Request) -> RunView:
        return views_of(request).get(request.path_params["run_id"])

    def run_page(request: Request) -> Response:
        try:
            view = get_view(request)
        except ReplayError as e:
            return page(request, "error.html", message=e.message, status_code=404)
        snap = view.snapshot()
        ctx = context(snap)
        ctx["panels"] = render_panels(templates, snap, set(PANELS))
        ctx["log_html"] = render_log(templates, snap["log"])
        return page(request, "run/run.html", **ctx)

    async def run_events(request: Request) -> Response:
        try:
            view = await run_in_threadpool(get_view, request)
        except ReplayError as e:
            return Response(e.message, status_code=404)
        return StreamingResponse(
            run_stream(request, templates, view, _after(request)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    def download(request: Request) -> Response:
        rel = request.path_params["path"]
        try:
            view = get_view(request)
        except ReplayError as e:
            return Response(e.message, status_code=404)
        view.poll()
        listed = {a["path"]: a for a in view.artifacts}
        full = safe_output_file(view.output_dir, rel) if rel in listed else None
        if full is None:
            return Response("Файла нет среди результатов этого запуска.", status_code=404, media_type="text/plain; charset=utf-8")
        mime = listed[rel].get("mime") or "application/octet-stream"
        return FileResponse(full, media_type=mime, filename=full.name)

    def stderr_log(request: Request) -> Response:
        """stderr.log запуска целиком, с той же маскировкой секретов, что и хвост на экране."""
        with request.app.state.ui.open_app() as app:
            try:
                rec = app.runs.get(request.path_params["run_id"])
            except ReplayError as e:
                return Response(e.message, status_code=404, media_type="text/plain; charset=utf-8")
            path = app.paths.run_dir(rec.id) / "stderr.log"
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            text = diagnosis.mask(text, diagnosis.known_secrets(app, rec.script_name))
        return Response(
            text or "stderr пуст\n",
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="stderr-{rec.id}.log"'},
        )

    async def open_dir(request: Request) -> Response:
        try:
            view = await run_in_threadpool(get_view, request)
        except ReplayError as e:
            return notice(request, e.message, ok=False)
        if not view.output_dir.is_dir():
            return notice(request, "Папки с результатами пока нет: скрипт ещё ничего не сохранил.", ok=False)
        try:
            await run_in_threadpool(open_folder, view.output_dir)
        except OSError as e:
            return notice(request, f"Не удалось открыть папку: {e}. Путь: {view.output_dir}", ok=False)
        return notice(request, f"Папка открыта: {view.output_dir}")

    def cancel(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        with request.app.state.ui.open_app() as app:
            try:
                launch.cancel(app, run_id, wait=False)
            except ReplayError as e:
                if e.code != "not_running":
                    return notice(request, e.message, ok=False)
        view = get_view(request)
        snap = view.snapshot()
        # Главная перерисует «Идут сейчас» по событию runs-changed.
        return Response(render_panels(templates, snap, {"head"})["head"], media_type="text/html; charset=utf-8",
                        headers={"HX-Trigger": "runs-changed"})

    async def release(request: Request) -> Response:
        form = await request.form()
        session_id = form.get("session_id") or None
        from_home = form.get("_from") == "home"
        run_id = request.path_params["run_id"]

        def work() -> tuple[list[str], bool]:
            with request.app.state.ui.open_app() as app:
                rec = app.runs.get(run_id)
                if rec.running:
                    raise ReplayError("run_running", "Запуск ещё идёт: браузеры после него проверит страховка.")
                key = app.secrets.get_api_key()
                if not key:
                    raise ReplayError("no_api_key", "Ключ DAWG не задан: добавьте его в настройках.")
                base_url = app.config.base_url or None
                try:
                    results = insurance.release_suspects(
                        app.paths.run_dir(run_id),
                        [str(session_id)] if session_id else None,
                        lambda sid: insurance.release_session(key, sid, base_url),
                    )
                except insurance.NotASuspect as e:
                    raise ReplayError("not_a_suspect", str(e)) from None
            lines = [
                f"Браузер {r.session_id} освобождён." if r.released else f"Браузер {r.session_id} не освобождён: {r.error}"
                for r in results
            ]
            return lines, all(r.released for r in results)

        try:
            lines, ok = await run_in_threadpool(work)
        except ReplayError as e:
            lines, ok = [e.message], False
        if from_home:
            body = templates.get_template("run/_notice.html").render(text=" ".join(lines), ok=ok)
            return Response(body, media_type="text/html; charset=utf-8", headers={"HX-Trigger": "runs-changed"})
        view = await run_in_threadpool(get_view, request)
        snap = await run_in_threadpool(view.snapshot)
        ctx = {**context(snap), "release_lines": lines, "release_ok": ok}
        return Response(templates.get_template("run/_suspects.html").render(ctx), media_type="text/html; charset=utf-8")

    return [
        Route("/runs/{run_id}", run_page),
        Route("/runs/{run_id}/events", run_events),
        Route("/runs/{run_id}/files/{path:path}", download),
        Route("/runs/{run_id}/stderr.log", stderr_log),
        Route("/runs/{run_id}/open-folder", open_dir, methods=["POST"]),
        Route("/runs/{run_id}/cancel", cancel, methods=["POST"]),
        Route("/runs/{run_id}/release", release, methods=["POST"]),
    ]


__all__ = ["routes", "markdown", "safe_output_file", "run_stream", "Views", "open_folder"]
