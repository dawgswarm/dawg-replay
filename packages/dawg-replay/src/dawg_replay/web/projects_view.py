"""Экраны проектов (M6): список, редактор схемы, страница проекта, запуск проекта.

Редактор (static/project.js) получает данные JSON-ом (`/projects/<имя>/_data`) и сохраняет проект
целиком (`POST /projects/<имя>/save`): каждое сохранение это новая версия, как правка файла агентом.
Если текущая версия уже не та, от которой шла правка (агент успел поправить файл), сохранение
отказывается, редактор предлагает загрузить свежую.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from dawg_replay import project_runs as P
from dawg_replay import projects as projects_mod
from dawg_replay import schedules, tg_rules
from dawg_replay.errors import ReplayError
from dawg_replay.web import project_data as D

MAX_UPLOAD = 1_000_000
TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
     "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))
KINDS = [("daily", "каждый день"), ("weekly", "раз в неделю"), ("hours", "каждые несколько часов"), ("cron", "cron")]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def slug(title: str) -> str:
    s = "".join(TRANSLIT.get(c, c) for c in title.strip().lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:48].strip("-") or "project"


def _ui(request: Request) -> Any:
    return request.app.state.ui


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def routes(templates: Jinja2Templates) -> list[Route]:
    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, {"active": "projects", **ctx}, status_code=status_code)

    def fragment(request: Request, name: str, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx)

    def error_json(e: ReplayError, status: int = 400) -> JSONResponse:
        problems = e.details if isinstance(e.details, list) else []
        return JSONResponse({"error": {"code": e.code, "message": _cap(e.message), "problems": problems}},
                            status_code=status)

    # --- список ---

    def projects_page(request: Request) -> Response:
        with _ui(request).open_app() as app:
            items = D.overview(app)
        notice = request.query_params.get("removed", "")[:80]
        return page(request, "projects/list.html", items=items, removed=notice)

    async def project_new(request: Request) -> Response:
        form = await request.form()
        title = str(form.get("title") or "").strip()[:120]

        def work() -> Response:
            if not title:
                return fragment(request, "_flash.html", message="Назовите проект: например, «Карточки WB в Google»")
            with _ui(request).open_app() as app:
                base = slug(title)
                name, i = base, 2
                while True:
                    try:
                        app.projects.get(name)
                    except ReplayError:
                        break
                    name, i = f"{base}-{i}", i + 1
            # Пустой проект не сохраняется (в проекте должен быть узел): холст открывается черновиком,
            # проект создаётся первым сохранением, когда на холсте появится узел.
            return Response(status_code=204, headers={"HX-Redirect": f"/projects/{quote(name)}?new=1&title={quote(title)}"})

        return await run_in_threadpool(work)

    async def project_import(request: Request) -> Response:
        form = await request.form()
        upload = form.get("file")
        data = await upload.read(MAX_UPLOAD + 1) if upload is not None and hasattr(upload, "read") else b""
        filename = getattr(upload, "filename", "") or "проект"

        def work() -> Response:
            if not data:
                return fragment(request, "projects/_import_result.html", error="Выберите файл проекта (.project.toml)")
            if len(data) > MAX_UPLOAD:
                return fragment(request, "projects/_import_result.html", error="Файл больше 1 МБ: это не файл проекта")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                return fragment(request, "projects/_import_result.html", error=f"{filename}: файл не в UTF-8")
            with _ui(request).open_app() as app:
                try:
                    project = projects_mod.parse(text, filename)
                    exists = True
                    try:
                        app.projects.get(project.name)
                    except ReplayError:
                        exists = False
                    r = app.projects.save(text, origin="file", create=not exists)
                except ReplayError as e:
                    problems = e.details if isinstance(e.details, list) else []
                    return fragment(request, "projects/_import_result.html", error=_cap(e.message.split("\n")[0]),
                                    problems=problems)
            return Response(status_code=204, headers={"HX-Redirect": f"/projects/{quote(r.info.name)}?imported={r.status}"})

        return await run_in_threadpool(work)

    # --- редактор ---

    def project_editor(request: Request) -> Response:
        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            draft = None
            try:
                info = app.projects.get(name).to_dict()
            except ReplayError as e:
                title = request.query_params.get("title", "").strip()[:120]
                if request.query_params.get("new") != "1" or not title or not projects_mod.NAME_RE.match(name):
                    return page(request, "error.html", status_code=404, message=_cap(e.message))
                info, draft = {"name": name, "title": title, "current_version": None}, True
            schedules_ = [s.spec.describe() for s in app.schedules.project_all(name)] if not draft else []
        return page(request, "projects/editor.html", info=info, schedules=schedules_, draft=draft,
                    imported=request.query_params.get("imported", "")[:20], body_class="pe-body")

    def project_data(request: Request) -> Response:
        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            try:
                return JSONResponse(D.editor(app, name))
            except ReplayError as e:
                if e.code == "project_not_found" and request.query_params.get("new") == "1":
                    title = request.query_params.get("title", "").strip()[:120] or name
                    return JSONResponse(D.draft(app, name, title))
                return error_json(e, 404)

    async def project_save(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse({"error": {"code": "bad_json", "message": "Не JSON", "problems": []}}, status_code=400)

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    project = D.project_from_json(body.get("project"))
                    if project.name != name:
                        raise ReplayError("bad_project", "имя проекта в редакторе менять нельзя")
                    base = body.get("base_version")
                    r = app.projects.save(project, base_version=base if isinstance(base, int) else None,
                                          origin="editor", create=bool(body.get("create")))
                    return JSONResponse({"status": r.status, "version": r.version.version,
                                         "problems": D.problems_json(app.projects.check(project))})
                except ReplayError as e:
                    return error_json(e, 409 if e.code == "project_changed" else 400)

        return await run_in_threadpool(work)

    async def project_check(request: Request) -> Response:
        """Проверка без сохранения: для подсказок на холсте во время правки."""
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    project = D.project_from_json(body.get("project"))
                    return JSONResponse({"problems": D.problems_json(app.projects.check(project))})
                except ReplayError as e:
                    return error_json(e)

        return await run_in_threadpool(work)

    def project_script(request: Request) -> Response:
        """Карточка скрипта для холста (после «Добавить скрипт» или правки пресетов в другой вкладке)."""
        with _ui(request).open_app() as app:
            card = D.script_card(app, request.path_params["script"])
        if card is None:
            return JSONResponse({"error": {"code": "script_not_found", "message": "Скрипт не найден", "problems": []}},
                                status_code=404)
        return JSONResponse(card)

    # --- запуск ---

    async def project_run(request: Request) -> Response:
        name = request.path_params["name"]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    pr = P.start(app, name)
                except ReplayError as e:
                    if e.code == "project_busy":
                        busy = P.running(app, name)
                        link = f"/projects/runs/{busy[0].id}" if busy else None
                        return fragment(request, "_flash.html", message="Проект уже запущен. Откройте идущий запуск или остановите его.",
                                        link=link, link_text="Открыть идущий запуск")
                    return fragment(request, "_flash.html", message=f"Запуск не начат: {_cap(e.message)}")
            return Response(status_code=204, headers={"HX-Redirect": f"/projects/runs/{pr.id}"})

        return await run_in_threadpool(work)

    def run_page(request: Request) -> Response:
        prun_id = request.path_params["prun_id"]
        with _ui(request).open_app() as app:
            try:
                data = D.run_page(app, prun_id)
            except ReplayError as e:
                return page(request, "error.html", status_code=404, message=_cap(e.message))
        return page(request, "projects/run.html", data=data, body_class="pe-body")

    def run_state(request: Request) -> Response:
        prun_id = request.path_params["prun_id"]
        with _ui(request).open_app() as app:
            try:
                pr = P.get(app, prun_id)
                return JSONResponse(D.run_json(app, pr))
            except ReplayError as e:
                return error_json(e, 404)

    async def run_stop(request: Request) -> Response:
        prun_id = request.path_params["prun_id"]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    P.stop(app, prun_id)
                except ReplayError as e:
                    return error_json(e)
                return JSONResponse(D.run_json(app, P.get(app, prun_id)))

        return await run_in_threadpool(work)

    async def run_retry(request: Request) -> Response:
        prun_id = request.path_params["prun_id"]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    pr = P.retry(app, prun_id)
                except ReplayError as e:
                    return error_json(e)
            return JSONResponse({"redirect": f"/projects/runs/{pr.id}"})

        return await run_in_threadpool(work)

    # --- страница проекта: расписание, Telegram, версии, история ---

    def _about(app: Any, name: str, **extra: Any) -> dict[str, Any]:
        info = app.projects.get(name)
        data = app.schedules.project_list(name)
        for s in data["schedules"]:
            s["tg"] = tg_rules.layers(app, "project_schedule", s["id"], name) if tg_rules.connected(app) else None
        history = []
        for pr in P.history(app, name, 30):
            d = D.run_json(app, pr)
            history.append(d)
        cron_ok = app.schedules.backend.kind == "cron"
        return {
            "info": info.to_dict(),
            "versions": [v.to_dict() for v in reversed(app.projects.versions(name))],
            "schedules": data["schedules"], "scheduler_error": data["error"],
            "kinds": KINDS if cron_ok else KINDS[:3], "weekdays": WEEKDAYS, "every_hours": schedules.EVERY_HOURS,
            "tg_connected": tg_rules.connected(app),
            "tg": tg_rules.layers(app, "project", name) if tg_rules.connected(app) else [],
            "manual": tg_rules.MANUAL_CHOICES,
            "history": history,
            **extra,
        }

    def about_page(request: Request) -> Response:
        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            try:
                ctx = _about(app, name)
            except ReplayError as e:
                return page(request, "error.html", status_code=404, message=_cap(e.message))
        return page(request, "projects/about.html", a=ctx)

    async def schedule_add(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        f = {k: str(form.get(k) or "").strip()[:200] for k in ("kind", "time", "weekday", "hours", "cron")}

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    s = app.schedules.add_project(name, _spec(f))
                except ReplayError as e:
                    return fragment(request, "projects/_schedules.html", a=_about(app, name, sched_error=_cap(e.message), form=f))
                return fragment(request, "projects/_schedules.html",
                                a=_about(app, name, sched_notice=f"Расписание добавлено: {s.spec.describe()}."))

        return await run_in_threadpool(work)

    async def schedule_delete(request: Request) -> Response:
        name, sid = request.path_params["name"], request.path_params["sid"]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    if app.schedules.project_get(sid).project != name:
                        raise ReplayError("schedule_not_found", "это расписание другого проекта")
                    app.schedules.remove(sid)
                except ReplayError as e:
                    return fragment(request, "projects/_schedules.html", a=_about(app, name, sched_error=_cap(e.message)))
                return fragment(request, "projects/_schedules.html", a=_about(app, name, sched_notice="Расписание снято."))

        return await run_in_threadpool(work)

    async def telegram_save(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        values = {}
        for k in tg_rules.PROJECT_KEYS:
            raw = str(form.get(k) or "").strip()
            values[k] = tg_rules.INHERIT if raw in ("", tg_rules.INHERIT) else raw

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    tg_rules.set_overrides(app, "project", name, values)
                except ReplayError as e:
                    return fragment(request, "projects/_telegram.html", a=_about(app, name, tg_error=_cap(e.message)))
                return fragment(request, "projects/_telegram.html", a=_about(app, name, tg_notice="Сохранено."))

        return await run_in_threadpool(work)

    async def rollback(request: Request) -> Response:
        name = request.path_params["name"]
        form = await request.form()
        try:
            version = int(str(form.get("version") or ""))
        except ValueError:
            return fragment(request, "_flash.html", message="Не указана версия")

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    app.projects.rollback(name, version)
                except ReplayError as e:
                    return fragment(request, "_flash.html", message=_cap(e.message))
            return Response(status_code=204, headers={"HX-Redirect": f"/projects/{quote(name)}/about#versions"})

        return await run_in_threadpool(work)

    def export(request: Request) -> Response:
        name = request.path_params["name"]
        with _ui(request).open_app() as app:
            try:
                text = app.projects.export(name).text
            except ReplayError as e:
                return page(request, "error.html", status_code=404, message=_cap(e.message))
        return Response(text, media_type="application/toml; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=\"{name}.project.toml\""})

    async def delete(request: Request) -> Response:
        name = request.path_params["name"]

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    title = app.projects.get(name).title
                    app.remove_project(name)
                except ReplayError as e:
                    return fragment(request, "_flash.html", message=_cap(e.message))
            return Response(status_code=204, headers={"HX-Redirect": f"/projects?removed={quote(title)}"})

        return await run_in_threadpool(work)

    # --- «Добавить в проект» с экрана запуска ---

    async def add_from_run(request: Request) -> Response:
        """Два узла и стрелка: скрипт запуска и получатель файла, в новый или существующий проект."""
        form = await request.form()
        f = {k: str(form.get(k) or "").strip()[:200] for k in ("run_id", "target", "file", "param", "project", "title")}

        def work() -> Response:
            with _ui(request).open_app() as app:
                try:
                    name = _add_pair(app, f)
                except ReplayError as e:
                    return fragment(request, "_flash.html", message=f"Не добавлено: {_cap(e.message)}")
            return Response(status_code=204, headers={"HX-Redirect": f"/projects/{quote(name)}"})

        return await run_in_threadpool(work)

    return [
        Route("/projects", projects_page),
        Route("/projects/_new", project_new, methods=["POST"]),
        Route("/projects/_import", project_import, methods=["POST"]),
        Route("/projects/_add_from_run", add_from_run, methods=["POST"]),
        Route("/projects/_script/{script}", project_script),
        Route("/projects/_check", project_check, methods=["POST"]),
        Route("/projects/runs/{prun_id}", run_page),
        Route("/projects/runs/{prun_id}/_state", run_state),
        Route("/projects/runs/{prun_id}/stop", run_stop, methods=["POST"]),
        Route("/projects/runs/{prun_id}/retry", run_retry, methods=["POST"]),
        Route("/projects/{name}", project_editor),
        Route("/projects/{name}/_data", project_data),
        Route("/projects/{name}/save", project_save, methods=["POST"]),
        Route("/projects/{name}/run", project_run, methods=["POST"]),
        Route("/projects/{name}/about", about_page),
        Route("/projects/{name}/schedules", schedule_add, methods=["POST"]),
        Route("/projects/{name}/schedules/{sid}/delete", schedule_delete, methods=["POST"]),
        Route("/projects/{name}/telegram", telegram_save, methods=["POST"]),
        Route("/projects/{name}/rollback", rollback, methods=["POST"]),
        Route("/projects/{name}/export", export),
        Route("/projects/{name}/delete", delete, methods=["POST"]),
    ]


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


def _add_pair(app: Any, f: dict[str, str]) -> str:
    """«Добавить в проект»: узел скрипта запуска, узел получателя и стрелка файл → параметр.
    Пресет узлов как у запуска (если запуск шёл с пресетом), иначе без пресета."""
    rec = app.runs.get(f["run_id"])
    target = f["target"]
    app.registry.get(target)
    if not f["file"] or not f["param"]:
        raise ReplayError("bad_params", "выберите файл и параметр, куда его передать")
    if f["project"]:
        stored = app.projects.load(f["project"])
        project, base, create = stored.project, stored.version.version, False
    else:
        title = f["title"] or f"{rec.script_name} → {target}"
        name, i = slug(title), 2
        while True:
            try:
                app.projects.get(name)
            except ReplayError:
                break
            name, i = f"{slug(title)}-{i}", i + 1
        project, base, create = projects_mod.Project(name=name, title=title), None, True
    ids = {n.id for n in project.nodes}

    def new_id(script: str) -> str:
        base_id = re.sub(r"[^A-Za-z0-9_-]+", "-", script).strip("-") or "node"
        nid, i = base_id, 2
        while nid in ids:
            nid, i = f"{base_id}-{i}", i + 1
        ids.add(nid)
        return nid

    preset = rec.preset if rec.preset else None
    if preset:
        try:
            app.get_preset(rec.script_name, preset)
        except ReplayError:
            preset = None
    y = max([n.y or 0 for n in project.nodes], default=-160) + 200
    src = projects_mod.Node(id=new_id(rec.script_name), script=rec.script_name, preset=preset, x=40, y=y)
    dst = projects_mod.Node(id=new_id(target), script=target, x=380, y=y)
    data = D.project_json(project)
    data["nodes"] += [src.to_dict(), dst.to_dict()]
    data["edges"].append({"from": src.id, "to": dst.id, "when": "partial", "if_file": None,
                          "map": [{"kind": "file", "file": f["file"], "value": None, "column": None,
                                   "param": f["param"], "rows": "first"}],
                          "source_column": None, "limit": None, "where": None, "parallel": 1, "on_error": "continue"})
    result = app.projects.save(D.project_from_json(data), base_version=base, origin="editor", create=create)
    return result.info.name
