"""Данные экранов проектов: список, редактор (холст), страница проекта, запуск проекта.

Редактор работает с JSON проекта в виде `Project.to_dict()`; у стрелки соответствия лежат списком
`map` (`{kind, file, value, column, param, rows}`, dr-120). Пока в приложении только файловые
соответствия (`files`), здесь они переводятся в `map` и обратно, значения и колонки отклоняются
понятной ошибкой.
"""

from __future__ import annotations

from typing import Any

from dawg_replay import outputs as outputs_mod
from dawg_replay import project_runs as P
from dawg_replay import projects as projects_mod
from dawg_replay.errors import ReplayError
from dawg_replay.web.home import cover, manifest_meta

MAP_KINDS = ("file", "value", "column")
ROWS = ("first", "each", "all")
LIST_TYPES = ("string_list", "url_list", "multi_enum")
# Простые типы правятся прямо на карточке узла, остальные в панели справа.
SIMPLE_TYPES = ("string", "text", "url", "int", "float", "enum", "bool")
TYPE_WORDS = {
    "string": "текст", "text": "текст", "url": "ссылка", "int": "число", "float": "число", "bool": "да/нет",
    "enum": "выбор", "multi_enum": "несколько из списка", "date": "дата", "time": "время", "datetime": "дата и время",
    "url_list": "список ссылок", "string_list": "список", "table": "таблица", "file": "файл", "secret": "секрет",
}


# --- список проектов -----------------------------------------------------------------------


def _last_run(app: Any, name: str) -> dict[str, Any] | None:
    runs = P.history(app, name, 1)
    if not runs:
        return None
    pr = runs[0]
    return {"id": pr.id, "status": pr.status, "label": pr.label, "created_at": pr.created_at,
            "finished_at": pr.finished_at, "running": pr.running, "trigger": pr.trigger}


def overview(app: Any) -> list[dict[str, Any]]:
    """Экран «Проекты»: проект, сколько узлов, расписания словами, последний запуск."""
    out = []
    for info in app.projects.list():
        try:
            project = app.projects.load(info.name).project
            nodes = len(project.nodes)
            scripts = [n.script for n in project.nodes]
        except ReplayError:
            nodes, scripts = 0, []
        covers = []
        for s in dict.fromkeys(scripts):
            try:
                title = app.registry.get(s).title
            except ReplayError:
                title = s
            covers.append(cover(title, manifest_meta(app, s)["site"]))
        out.append({
            "project": info.to_dict(),
            "nodes": nodes,
            "covers": covers[:4],
            "schedules": [s.spec.describe() for s in app.schedules.project_all(info.name)],
            "last_run": _last_run(app, info.name),
        })
    return out


# --- палитра и описание скриптов для холста ------------------------------------------------


def _param(p: Any) -> dict[str, Any]:
    t = "secret" if p.type == "secret" else p.type
    d: dict[str, Any] = {
        "name": p.name, "title": p.title or p.name, "type": t, "type_word": TYPE_WORDS.get(t, t),
        "required": bool(p.required), "default": p.default, "secret": bool(p.is_secret),
        "transfer": p.type in ("table", "file") and not p.is_secret,  # файл по стрелке
        "simple": t in SIMPLE_TYPES and not p.is_secret,
        "list": t in LIST_TYPES,
    }
    if p.choices is not None:
        d["choices"] = [{"value": v, "title": p.choice_title(v)} for v in p.choices]
    if p.columns:
        d["columns"] = list(p.columns)
    if getattr(p, "multiple", False):
        d["multiple"] = True
    return d


def _outputs(app: Any, name: str) -> list[dict[str, Any]]:
    """Выходы: файлы (объявленные или из последнего запуска) и значения (kit 1.5, dr-119)."""
    try:
        so = outputs_mod.script_outputs(app, name)
    except ReplayError:
        return []
    out = []
    for o in so.get("outputs") or []:
        if o.get("pattern"):
            continue  # по шаблону не ясно, какой файл; стрелку на шаблон ставит агент в файле проекта
        out.append({"name": o["name"], "kind": "file", "title": o.get("title"), "columns": o.get("columns"),
                    "main": bool(o.get("main")), "optional": bool(o.get("optional"))})
    for v in so.get("values") or []:
        out.append({"name": v["name"], "kind": "value", "title": v.get("title"), "type": v.get("type"),
                    "type_word": TYPE_WORDS.get(v.get("type") or "", v.get("type") or "значение"),
                    "optional": bool(v.get("optional")), "secret": bool(v.get("secret"))})
    return out


def _preset_view(app: Any, script: str, manifest: Any, name: str) -> dict[str, Any]:
    """Пресет для карточки: несекретные значения как есть, секретные отмечены."""
    try:
        pr = app.presets.get(script, name, manifest)
    except ReplayError:
        return {"name": name, "values": {}, "secret": [], "files": {}}
    secret = {p.name for p in manifest.params if p.is_secret}
    values = {k: v for k, v in pr.params.items() if k not in secret}
    filled_secret = sorted((set(pr.params) & secret) | set(pr.secret_tables))
    files = {**{k: v for k, v in pr.files.items()}, **{k: v for k, v in pr.results.items()},
             **{k: ", ".join(f.get("name", "") for f in v) for k, v in pr.file_params.items()}}
    return {"name": pr.name, "values": values, "secret": filled_secret, "files": files}


def script_card(app: Any, name: str) -> dict[str, Any] | None:
    """Всё о скрипте, что нужно холсту: входы, выходы, пресеты."""
    try:
        s = app.registry.get(name)
        m = app.registry.manifest(name)
    except ReplayError:
        return None
    meta = manifest_meta(app, name)
    presets = []
    try:
        names = [p.name for p in app.list_presets(name)]
    except ReplayError:
        names = []
    for pname in names:
        presets.append(_preset_view(app, name, m, pname))
    return {
        "name": name, "title": s.title, "description": s.description, "site": meta["site"],
        "builtin": bool(getattr(s, "builtin", False)), "cover": cover(s.title, meta["site"]),
        "params": [_param(p) for p in m.params], "outputs": _outputs(app, name), "presets": presets,
    }


def palette(app: Any) -> list[dict[str, Any]]:
    out = []
    for s in app.registry.list():
        card = script_card(app, s.name)
        if card is not None:
            out.append(card)
    return out


# --- проект <-> JSON редактора ---------------------------------------------------------------


def edge_json(e: Any) -> dict[str, Any]:
    d = e.to_dict()
    if "map" not in d:  # до dr-120: только файлы
        d["map"] = [{"kind": "file", "file": f["file"], "value": None, "column": None, "param": f["param"],
                     "rows": "first"} for f in d.pop("files", [])]
    d.setdefault("limit", None)
    d.setdefault("where", None)
    d.setdefault("parallel", 1)
    d.setdefault("on_error", "continue")
    return d


def project_json(project: Any) -> dict[str, Any]:
    d = project.to_dict()
    d["edges"] = [edge_json(e) for e in project.edges]
    return d


def _clean_values(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        return {}
    return {str(k): v for k, v in values.items() if v is not None and v != ""}


def project_from_json(data: Any) -> Any:
    """JSON редактора в Project. Проверка устройства та же, что у файла (projects.parse)."""
    if not isinstance(data, dict):
        raise ReplayError("bad_project", "проект должен быть объектом JSON")
    if hasattr(projects_mod, "from_dict"):  # dr-120
        return projects_mod.from_dict(data)
    nodes = []
    for n in data.get("nodes") or []:
        if not isinstance(n, dict):
            raise ReplayError("bad_project", "узел должен быть объектом")
        nodes.append(projects_mod.Node(
            id=str(n.get("id") or ""), script=str(n.get("script") or ""), preset=n.get("preset") or None,
            values=_clean_values(n.get("values")), title=(n.get("title") or None),
            x=_num(n.get("x")), y=_num(n.get("y")), wait=n.get("wait") or "all"))
    edges = []
    for e in data.get("edges") or []:
        if not isinstance(e, dict):
            raise ReplayError("bad_project", "стрелка должна быть объектом")
        files = []
        for m in e.get("map") or e.get("files") or []:
            kind = m.get("kind", "file")
            if kind != "file":
                raise ReplayError("not_supported", "передача значений и колонок по стрелке появится вместе с kit 1.5; "
                                                   "пока по стрелке идут только файлы")
            files.append(projects_mod.FileMap(file=str(m.get("file") or ""), param=str(m.get("param") or "")))
        edges.append(projects_mod.Edge(
            source=str(e.get("from") or ""), target=str(e.get("to") or ""), when=e.get("when") or "partial",
            if_file=e.get("if_file") or None, files=tuple(files), source_column=e.get("source_column") or None))
    project = projects_mod.Project(
        name=str(data.get("name") or ""), title=str(data.get("title") or ""),
        description=data.get("description") or None, timeout_minutes=data.get("timeout_minutes") or None,
        nodes=tuple(nodes), edges=tuple(edges))
    # Через текст: та же проверка устройства, что у файла проекта.
    return projects_mod.parse(projects_mod.dump(project), "проект")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return round(float(v), 1)


def problems_json(problems: list[Any]) -> list[dict[str, Any]]:
    return [p.to_dict() for p in problems]


def editor(app: Any, name: str, version: int | None = None) -> dict[str, Any]:
    """Всё для холста: проект, версия, проблемы проверки, скрипты, последний запуск."""
    stored = app.projects.load(name, version)
    project = stored.project
    scripts = {s["name"]: s for s in palette(app)}
    for n in project.nodes:  # узел со снятым скриптом: карточка без входов, проверка покажет ошибку
        scripts.setdefault(n.script, None)
    last = P.history(app, name, 1)
    return {
        "project": project_json(project),
        "version": stored.version.version,
        "current": stored.info.current_version,
        "versions": stored.info.versions_count,
        "problems": problems_json(app.projects.check(project)),
        "scripts": scripts,
        "last_run": run_json(app, last[0], project) if last else None,
        "schedules": [s.spec.describe() for s in app.schedules.project_all(name)],
        "titles": {n.id: app.projects.node_title(n) for n in project.nodes},
    }


def draft(app: Any, name: str, title: str) -> dict[str, Any]:
    """Новый проект до первого узла: в базе его ещё нет."""
    return {
        "project": {"name": name, "title": title, "description": None, "timeout_minutes": None, "nodes": [], "edges": []},
        "version": None, "current": None, "versions": 0, "problems": [], "draft": True,
        "scripts": {s["name"]: s for s in palette(app)}, "last_run": None, "schedules": [], "titles": {},
    }


# --- запуск проекта ----------------------------------------------------------------------------


def run_json(app: Any, pr: Any, project: Any | None = None) -> dict[str, Any]:
    """Состояние запуска проекта для холста: статус каждого шага, причины, запуски строк."""
    d = pr.to_dict()
    steps = {}
    for s in d["steps"]:
        steps[s["node"]] = s
    d["steps_by_node"] = steps
    if project is not None:
        d["titles"] = {n.id: P.node_title(app, project, n.id) for n in project.nodes}
    total = len(pr.steps)
    ok = sum(1 for s in pr.steps if s.status in (P.OK,))
    d["summary"] = f"{ok} из {total} готово" if total else ""
    d["can_retry"] = pr.status in (P.FAILED, P.PARTIAL, P.CANCELLED) and not pr.running
    return d


def run_page(app: Any, prun_id: str) -> dict[str, Any]:
    pr = P.get(app, prun_id)
    stored = app.projects.load(pr.project, pr.version)
    scripts = {}
    for n in stored.project.nodes:
        if n.script not in scripts:
            scripts[n.script] = script_card(app, n.script)
    return {
        "project": project_json(stored.project),
        "info": app.projects.get(pr.project).to_dict(),
        "version": pr.version,
        "scripts": scripts,
        "run": run_json(app, pr, stored.project),
    }
