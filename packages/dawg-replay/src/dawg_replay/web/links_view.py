"""Экран «Связи»: карта, панели связи и получателя, раскладка; блок связей на странице скрипта."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from dawg_replay import links
from dawg_replay.errors import ReplayError
from dawg_replay.web import links_data

LAYOUT_BODY_MAX = 256 * 1024


def _problems_of(e: ReplayError) -> list[dict[str, Any]]:
    return [p for p in (e.details or []) if isinstance(p, dict)] if isinstance(e.details, list) else []


def _error_text(e: ReplayError) -> str:
    probs = [p["text"] for p in _problems_of(e) if p.get("level") == "error"]
    if probs:
        return "Связь не сохранена: " + "; ".join(probs)
    return e.message[:1].upper() + e.message[1:]


def routes(templates: Jinja2Templates, form_reader: Any) -> list[Route]:
    def render(request: Request, name: str, **ctx: Any) -> Response:
        return templates.TemplateResponse(request, name, ctx)

    def open_app(request: Request):
        return request.app.state.ui.open_app()

    def page(request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        focus = request.query_params.get("script") or None
        link_id = request.query_params.get("link") or None
        with open_app(request) as app:
            m = links_data.map_view(app, show_all=show_all)
            panel = None
            if link_id and link_id.isdigit():
                try:
                    panel = {"kind": "link", "p": links_data.link_panel(app, int(link_id))}
                except ReplayError:
                    panel = None
            elif focus:
                try:
                    panel = {"kind": "target", "p": links_data.target_panel(app, focus)}
                except ReplayError:
                    panel = None
        return templates.TemplateResponse(request, "links.html", {"active": "links", "m": m, "panel": panel,
                                                                  "focus": focus})

    def map_fragment(request: Request) -> Response:
        show_all = request.query_params.get("all") == "1"
        with open_app(request) as app:
            m = links_data.map_view(app, show_all=show_all)
        return render(request, "links/_map.html", m=m)

    def _link_panel(request: Request, link_id: int, refresh: bool = False, **kw: Any) -> Response:
        with open_app(request) as app:
            try:
                p = links_data.link_panel(app, link_id, **kw)
            except ReplayError as e:
                return render(request, "links/_gone.html", message=e.message)
        resp = render(request, "links/_link_panel.html", p=p)
        if kw.get("saved") or refresh:
            resp.headers["HX-Trigger"] = "links-changed"
        return resp

    def link_panel(request: Request) -> Response:
        return _link_panel(request, int(request.path_params["link_id"]))

    async def link_create(request: Request) -> Response:
        form = await form_reader(request)
        source = (form.get("source") or "").strip()
        target = (form.get("target") or "").strip()
        after = form.get("after") or "ok"
        back = form.get("back") or ""
        with open_app(request) as app:
            try:
                if not source or not target:
                    raise ReplayError("bad_link", "выберите оба скрипта")
                if source == target:
                    raise ReplayError("bad_link", "скрипт не может запускать сам себя: выберите другой")
                existing = app.links.find(source, target)
                if existing is not None:
                    link, warnings = existing, []
                else:
                    files: list[links.Transfer] = []
                    if after == "never":
                        # «Только файл» без передачи не сохраняется: первая подходящая пара сразу.
                        params = links_data.eligible_params(app, target)
                        if params:
                            files = [links.Transfer(links.MAIN, params[0]["name"])]
                    link, warnings = app.links.add(source, target, cond="ok" if after == "never" else after,
                                                   starts=after != "never", files=files)
            except ReplayError as e:
                if back == "script":
                    return render(request, "links/_script_block.html",
                                  b=_block(request, target if form.get("side") == "in" else source), error=_error_text(e))
                return render(request, "links/_notice.html", error=_error_text(e))
        if back == "script":
            return Response(status_code=204, headers={"HX-Redirect": f"/links?link={link.id}"})
        resp = _link_panel(request, link.id, warnings=[w.to_dict() for w in warnings],
                           saved="Связь создана. Задайте передачи файлов и условие запуска." if existing is None else None,
                           refresh=True)
        return resp

    async def link_save(request: Request) -> Response:
        link_id = int(request.path_params["link_id"])
        form = await form_reader(request)
        with open_app(request) as app:
            try:
                args = links_data.parse_link_form(form)
                _, warnings = app.links.update(link_id, **args)
            except ReplayError as e:
                if e.code == "link_not_found":
                    return render(request, "links/_gone.html", message=e.message)
                p = links_data.link_panel(app, link_id, error=_error_text(e), form=dict(form))
                return render(request, "links/_link_panel.html", p=p)
        return _link_panel(request, link_id, warnings=[w.to_dict() for w in warnings], saved="Сохранено.")

    def link_toggle(request: Request) -> Response:
        link_id = int(request.path_params["link_id"])
        with open_app(request) as app:
            try:
                link = app.links.get(link_id)
                app.links.set_enabled(link_id, not link.enabled)
            except ReplayError as e:
                return render(request, "links/_gone.html", message=e.message)
        return _link_panel(request, link_id, saved="Связь включена." if not link.enabled else "Связь выключена.")

    def link_delete(request: Request) -> Response:
        link_id = int(request.path_params["link_id"])
        with open_app(request) as app:
            try:
                link = app.links.remove(link_id)
            except ReplayError as e:
                return render(request, "links/_gone.html", message=e.message)
        resp = render(request, "links/_gone.html", message=f"Связь {link.source} → {link.target} удалена.", ok=True)
        resp.headers["HX-Trigger"] = "links-changed"
        return resp

    def target_panel(request: Request) -> Response:
        name = request.path_params["name"]
        with open_app(request) as app:
            try:
                p = links_data.target_panel(app, name)
            except ReplayError as e:
                return render(request, "links/_gone.html", message=e.message)
        return render(request, "links/_target_panel.html", p=p)

    async def target_save(request: Request) -> Response:
        name = request.path_params["name"]
        form = await form_reader(request)
        with open_app(request) as app:
            try:
                args = links_data.parse_target_form(form)
                app.links.set_mode(name, **args)
            except ReplayError as e:
                if e.code == "script_not_found":
                    return render(request, "links/_gone.html", message=e.message)
                return render(request, "links/_target_panel.html",
                              p=links_data.target_panel(app, name, error=_error_text(e)))
            p = links_data.target_panel(app, name, saved="Сохранено.")
        resp = render(request, "links/_target_panel.html", p=p)
        resp.headers["HX-Trigger"] = "links-changed"
        return resp

    async def layout_save(request: Request) -> Response:
        body = await request.body()
        if len(body) > LAYOUT_BODY_MAX:
            return JSONResponse({"error": "слишком большая раскладка"}, status_code=413)
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return JSONResponse({"error": "нужен JSON"}, status_code=400)
        with open_app(request) as app:
            try:
                links_data.save_layout(app, data.get("positions") if isinstance(data, dict) else None)
            except ReplayError as e:
                return JSONResponse({"error": e.message}, status_code=400)
            except OSError as e:
                return JSONResponse({"error": f"раскладка не сохранена: {e.strerror or e}"}, status_code=500)
        return Response(status_code=204)

    def _block(request: Request, name: str) -> dict[str, Any]:
        with open_app(request) as app:
            return links_data.script_block(app, name)

    def script_block(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            b = _block(request, name)
        except ReplayError as e:
            return render(request, "links/_gone.html", message=e.message)
        return render(request, "links/_script_block.html", b=b)

    return [
        Route("/links", page),
        Route("/links", link_create, methods=["POST"]),
        Route("/links/_map", map_fragment),
        Route("/links/_layout", layout_save, methods=["POST"]),
        Route("/links/{link_id:int}", link_panel),
        Route("/links/{link_id:int}", link_save, methods=["POST"]),
        Route("/links/{link_id:int}/toggle", link_toggle, methods=["POST"]),
        Route("/links/{link_id:int}/delete", link_delete, methods=["POST"]),
        Route("/links/target/{name}", target_panel),
        Route("/links/target/{name}", target_save, methods=["POST"]),
        Route("/scripts/{name}/_links", script_block),
    ]


__all__ = ["routes"]
