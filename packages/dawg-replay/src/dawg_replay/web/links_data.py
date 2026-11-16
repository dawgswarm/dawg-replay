"""Данные экрана «Связи» и блоков связей: карта, панели связи и получателя, провенанс запуска.

Только чтение ядра (dawg_replay.links, outputs, presets, runs); шаблоны получают готовые словари.
"""

from __future__ import annotations

import json
import os
from typing import Any

from dawg_replay import links, outputs
from dawg_replay.errors import ReplayError

LAYOUT_FILE = "links-layout.json"  # раскладка узлов карты: {скрипт: [x, y]}
COL, ROW = 280, 128  # шаг автоматической раскладки
MAX_XY = 20000
AFTER_TITLES = (
    ("ok", "после успешного"),
    ("partial", "после успешного или частичного"),
    ("any", "после любого исхода"),
    ("never", "не запускать, только файл"),
)
AFTER_SHORT = {"ok": "после успешного", "partial": "после успешного или частичного", "any": "после любого исхода",
               "never": "только файл"}
MODE_TITLES = {"all": "после всех", "any": "после любого"}
TIMEOUT_TITLES = (("skip", "пропустить запуск"), ("run", "запустить с тем, что есть"))


# --- раскладка -----------------------------------------------------------------------


def load_layout(app: Any) -> dict[str, tuple[int, int]]:
    try:
        data = json.loads((app.paths.home / LAYOUT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, tuple[int, int]] = {}
    if isinstance(data, dict):
        for name, xy in data.items():
            if isinstance(name, str) and isinstance(xy, list) and len(xy) == 2 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in xy
            ):
                out[name] = (_clamp(xy[0]), _clamp(xy[1]))
    return out


def _clamp(v: float) -> int:
    return max(0, min(MAX_XY, int(v)))


def save_layout(app: Any, positions: Any) -> dict[str, tuple[int, int]]:
    """Сохраняет положения узлов, перетащенных мышкой. Незарегистрированные скрипты отбрасываются."""
    if not isinstance(positions, dict):
        raise ReplayError("bad_layout", "раскладка: нужен объект {скрипт: [x, y]}")
    known = {s.name for s in app.registry.list()}
    layout = {k: v for k, v in load_layout(app).items() if k in known}
    for name, xy in positions.items():
        if name in known and isinstance(xy, list) and len(xy) == 2 and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in xy
        ):
            layout[name] = (_clamp(xy[0]), _clamp(xy[1]))
    path = app.paths.home / LAYOUT_FILE
    tmp = path.with_name(f"{LAYOUT_FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({k: list(v) for k, v in layout.items()}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return layout


def _auto_layout(names: list[str], edges: list[tuple[str, str]]) -> dict[str, tuple[int, int]]:
    """Столбцы по самому длинному пути от источников; круги разрываются."""
    succ: dict[str, list[str]] = {n: [] for n in names}
    has_in: set[str] = set()
    for a, b in edges:
        if a in succ and b in succ and a != b:
            succ[a].append(b)
            has_in.add(b)
    rank: dict[str, int] = {n: 0 for n in names}
    seen: set[str] = set()
    # Сначала от скриптов без входящих стрелок, потом от оставшихся (круг без входа извне).
    for start in [n for n in names if n not in has_in] + names:
        if start in seen:
            continue
        stack = [(start, 0, (start,))]
        while stack:
            node, r, path = stack.pop()
            if node in seen and r <= rank[node]:
                continue
            seen.add(node)
            rank[node] = r
            for nxt in succ[node]:
                if nxt not in path:
                    stack.append((nxt, r + 1, (*path, nxt)))
    rows: dict[int, int] = {}
    out: dict[str, tuple[int, int]] = {}
    for n in sorted(names, key=lambda x: (rank[x], x)):
        r = rank[n]
        out[n] = (24 + r * COL, 24 + rows.get(r, 0) * ROW)
        rows[r] = rows.get(r, 0) + 1
    return out


# --- карта -----------------------------------------------------------------------------


def _title(app: Any, name: str) -> str:
    try:
        return app.registry.get(name).title or name
    except ReplayError:
        return name


def preset_refs(app: Any) -> list[dict[str, Any]]:
    """Ссылки на результат в пресетах (пунктирные стрелки на карте)."""
    out: list[dict[str, Any]] = []
    for script in app.registry.list():
        try:
            presets = app.list_presets(script.name)
        except ReplayError:
            continue
        for p in presets:
            for param, text in sorted(p.results.items()):
                ref = links.parse_ref(text)
                if ref is None:
                    continue
                source = ref.script
                if ref.run_id:
                    try:
                        source = app.runs.get(ref.run_id).script_name
                    except ReplayError:
                        continue
                if not source:
                    continue
                shown = "главный файл" if ref.file == links.MAIN else ref.file
                out.append({"source": source, "target": script.name, "preset": p.name, "param": param,
                            "file": ref.file, "label": f"{shown} → {param}", "ref": text})
    return out


def _node(app: Any, name: str) -> dict[str, Any]:
    node: dict[str, Any] = {"name": name, "title": _title(app, name), "broken": None, "schedule": None,
                            "mode": None, "run": None}
    try:
        app.registry.manifest(name)
    except ReplayError as e:
        node["broken"] = e.message
    scheds = app.schedules.all(name)
    if scheds:
        node["schedule"] = scheds[0].spec.describe() + (f" и ещё {len(scheds) - 1}" if len(scheds) > 1 else "")
    starting = [x for x in app.links.incoming(name) if x.enabled and x.starts]
    if len(starting) > 1:
        node["mode"] = MODE_TITLES.get(app.links.mode(name).mode)
    recent = app.runs.list(name, 1)
    if recent:
        r = recent[0]
        node["run"] = {"id": r.id, "label": r.label, "summary": r.summary, "created_at": r.created_at}
    return node


def map_view(app: Any, show_all: bool = False) -> dict[str, Any]:
    """Узлы, стрелки и ошибки схемы. Узлы: скрипты со связями или ссылками, с `show_all` все."""
    all_links = app.links.list()
    refs = preset_refs(app)
    problems = app.links.check()
    by_link: dict[int, list[dict[str, Any]]] = {}
    general: list[dict[str, Any]] = []
    for p in problems:
        d = p.to_dict()
        if p.link_id is None:
            general.append(d)
        else:
            by_link.setdefault(p.link_id, []).append(d)
    registered = [s.name for s in app.registry.list()]
    involved = {x.source for x in all_links} | {x.target for x in all_links}
    involved |= {r["source"] for r in refs} | {r["target"] for r in refs}
    names = [n for n in registered if show_all or n in involved]
    names += sorted(n for n in involved if n not in registered)
    edges: list[dict[str, Any]] = []
    for x in all_links:
        probs = by_link.get(x.id, [])
        level = "error" if any(p["level"] == "error" for p in probs) else ("warning" if probs else None)
        label = ", ".join(t.label for t in x.files) or "только после"
        if x.files and x.starts:
            label = f"{label}, {AFTER_SHORT[x.after]}" if x.after != "ok" else label
        edges.append({"kind": "link", "id": x.id, "source": x.source, "target": x.target, "dashed": not x.starts,
                      "enabled": x.enabled, "label": label, "level": level, "problems": probs})
    for i, r in enumerate(refs):
        edges.append({"kind": "ref", "id": f"ref-{i}", "source": r["source"], "target": r["target"], "dashed": True,
                      "enabled": True, "label": f"пресет «{r['preset']}»: {r['label']}", "level": None,
                      "problems": [], "preset": r["preset"]})
    layout = load_layout(app)
    auto = _auto_layout(names, [(e["source"], e["target"]) for e in edges])
    nodes = []
    for n in names:
        node = _node(app, n)
        node["x"], node["y"] = layout.get(n, auto[n])
        node["placed"] = n in layout
        nodes.append(node)
    return {"nodes": nodes, "edges": edges, "problems": general, "all_problems": [p.to_dict() for p in problems],
            "show_all": show_all, "scripts": [{"name": n, "title": _title(app, n)} for n in registered],
            "empty": not all_links and not refs}


# --- панели ----------------------------------------------------------------------------


def eligible_params(app: Any, name: str) -> list[dict[str, Any]]:
    """Параметры получателя, в которые можно передать файл: несекретные table и file."""
    try:
        m = app.registry.manifest(name)
    except ReplayError:
        return []
    out = []
    for p in m.params:
        if p.type in links.TRANSFER_TYPES and not p.secret:
            kind = "таблица" if p.type == "table" else ("файлы" if p.multiple else "файл")
            out.append({"name": p.name, "title": p.title, "type": p.type, "kind": kind,
                        "columns": list(p.columns) if p.columns else None})
    return out


def file_choices(app: Any, source: str) -> dict[str, Any]:
    """Что предлагать в поле «файл»: объявленные выходы или файлы последнего запуска, плюс главный файл."""
    try:
        got = outputs.script_outputs(app, source)
    except ReplayError:
        got = {"source": "none", "run_id": None, "outputs": []}
    got["main_offered"] = True
    return got


def link_panel(app: Any, link_id: int, *, warnings: list[dict[str, Any]] | None = None,
               error: str | None = None, saved: str | None = None, form: dict[str, Any] | None = None) -> dict[str, Any]:
    link = app.links.get(link_id)
    probs = [p.to_dict() for p in app.links.check(link.target) if p.link_id == link.id]
    try:
        presets = [p.name for p in app.list_presets(link.target)]
    except ReplayError:
        presets = []
    last = app.links.last_suitable_run(link.source, link.cond)
    journal = app.links.journal(link.id)
    values = {"after": link.after, "preset": link.preset or "", "limit": link.limit_per_day or "",
              "enabled": link.enabled, "files": [t.to_dict() for t in link.files]}
    if form is not None:  # ошибка сохранения: показать введённое, а не сохранённое
        rows = []
        i = 0
        while f"file_{i}" in form or f"param_{i}" in form:
            rows.append({"file": str(form.get(f"file_{i}") or ""), "param": str(form.get(f"param_{i}") or "")})
            i += 1
        values = {"after": str(form.get("after") or link.after), "preset": str(form.get("preset") or ""),
                  "limit": str(form.get("limit_per_day") or ""), "enabled": form.get("enabled") == "on",
                  "files": [r for r in rows if r["file"] or r["param"]]}
    return {
        "link": link.to_dict(),
        "source_title": _title(app, link.source),
        "target_title": _title(app, link.target),
        "after_titles": AFTER_TITLES,
        "params": eligible_params(app, link.target),
        "choices": file_choices(app, link.source),
        "presets": presets,
        "vals": values,
        "problems": probs,
        "warnings": warnings or [],
        "error": error,
        "saved": saved,
        "last": {"id": last.id, "created_at": last.finished_at or last.created_at} if last else None,
        "journal": journal,
        "main": links.MAIN,
    }


def target_panel(app: Any, name: str, *, error: str | None = None, saved: str | None = None) -> dict[str, Any]:
    app.registry.get(name)
    mode = app.links.mode(name)
    incoming = app.links.incoming(name)
    hours = mode.wait_seconds / 3600
    return {
        "name": name,
        "title": _title(app, name),
        "mode": mode.to_dict(),
        "wait_hours": int(hours) if hours == int(hours) else round(hours, 2),
        "mode_titles": MODE_TITLES,
        "timeout_titles": TIMEOUT_TITLES,
        "incoming": [{"id": x.id, "source": x.source, "source_title": _title(app, x.source), "after": AFTER_SHORT[x.after],
                      "enabled": x.enabled} for x in incoming],
        "outgoing": [{"id": x.id, "target": x.target, "target_title": _title(app, x.target), "after": AFTER_SHORT[x.after],
                      "enabled": x.enabled} for x in app.links.outgoing(name)],
        "starting": sum(1 for x in incoming if x.enabled and x.starts),
        "error": error,
        "saved": saved,
    }


def parse_link_form(form: Any) -> dict[str, Any]:
    """Поля формы связи в аргументы Links.update: after, файлы file_N/param_N, пресет, предел, вкл."""
    after = str(form.get("after") or "ok")
    if after not in links.AFTER_CHOICES:
        raise ReplayError("bad_link", "выберите, когда запускать получателя")
    transfers: list[links.Transfer] = []
    i = 0
    while f"file_{i}" in form or f"param_{i}" in form:
        file = str(form.get(f"file_{i}") or "").strip()
        param = str(form.get(f"param_{i}") or "").strip()
        if file and param:
            transfers.append(links.Transfer(file, param))
        elif file or param:
            raise ReplayError("bad_link", f"передача {i + 1}: укажите и файл, и параметр")
        i += 1
    limit_text = str(form.get("limit_per_day") or "").strip()
    limit: int | None = None
    if limit_text:
        try:
            limit = int(limit_text)
        except ValueError:
            raise ReplayError("bad_link", "предел запусков в сутки: целое число от 1") from None
        if limit < 1:
            raise ReplayError("bad_link", "предел запусков в сутки: целое число от 1")
    return {
        "cond": "ok" if after == "never" else after,
        "starts": after != "never",
        "files": transfers,
        "preset": str(form.get("preset") or "").strip() or None,
        "limit_per_day": limit,
        "enabled": str(form.get("enabled") or "") == "on",
    }


def parse_target_form(form: Any) -> dict[str, Any]:
    mode = str(form.get("mode") or "all")
    on_timeout = str(form.get("on_timeout") or "skip")
    text = str(form.get("wait_hours") or "").strip().replace(",", ".")
    try:
        hours = float(text)
    except ValueError:
        raise ReplayError("bad_link", "срок ожидания: число часов, например 6 или 0.5") from None
    return {"mode": mode, "on_timeout": on_timeout, "wait_seconds": int(round(hours * 3600))}


# --- блок на странице скрипта и провенанс запуска -----------------------------------


def script_block(app: Any, name: str) -> dict[str, Any]:
    """«Откуда берёт данные → этот скрипт → что запускает после» для страницы скрипта."""
    probs = app.links.check(name)
    by_link: dict[int, str] = {}
    for p in probs:
        if p.link_id is not None and (p.level == "error" or p.link_id not in by_link):
            by_link[p.link_id] = p.level

    def item(x: links.Link, other: str) -> dict[str, Any]:
        return {"id": x.id, "script": other, "title": _title(app, other), "after": AFTER_SHORT[x.after],
                "files": [t.label for t in x.files], "enabled": x.enabled, "starts": x.starts,
                "level": by_link.get(x.id)}

    incoming = [item(x, x.source) for x in app.links.incoming(name)]
    outgoing = [item(x, x.target) for x in app.links.outgoing(name)]
    refs = [r for r in preset_refs(app) if name in (r["source"], r["target"])]
    starting = sum(1 for x in app.links.incoming(name) if x.enabled and x.starts)
    others = [{"name": s.name, "title": s.title or s.name} for s in app.registry.list() if s.name != name]
    return {
        "name": name,
        "incoming": incoming,
        "outgoing": outgoing,
        "refs_in": [{**r, "title": _title(app, r["source"])} for r in refs if r["target"] == name],
        "refs_out": [{**r, "title": _title(app, r["target"])} for r in refs if r["source"] == name],
        "mode": MODE_TITLES.get(app.links.mode(name).mode) if starting > 1 else None,
        "others": others,
        "has_any": bool(incoming or outgoing or refs),
    }


def provenance(app: Any, rec: Any) -> dict[str, Any]:
    """Экран запуска: что его запустило и откуда файлы («Запущен после»), куда он передал («Передал в»)."""
    after = None
    if rec.link_id is not None or rec.source_run:
        src = None
        if rec.source_run:
            try:
                src = app.runs.get(rec.source_run)
            except ReplayError:
                src = None
        script = src.script_name if src else None
        if script is None and rec.link_id is not None:
            try:
                script = app.links.get(rec.link_id).source
            except ReplayError:
                script = None
        after = {"script": script, "title": _title(app, script) if script else None,
                 "run_id": src.id if src else rec.source_run,
                 "created_at": src.created_at if src else None, "link_id": rec.link_id}
    inputs = []
    for e in rec.inputs or []:
        if not isinstance(e, dict):
            continue
        src_script = e.get("source")
        inputs.append({"param": e.get("param"), "script": src_script,
                       "title": _title(app, src_script) if src_script else None, "run_id": e.get("run"),
                       "files": e.get("files") or [], "file": e.get("file")})
    passed = []
    for r in app.runs.fed_by(rec.id):
        passed.append({"id": r.id, "script": r.script_name, "title": _title(app, r.script_name), "label": r.label,
                       "created_at": r.created_at, "started": r.source_run == rec.id})
    return {"after": after, "inputs": inputs, "passed": passed}


# --- «Передать в другой скрипт» --------------------------------------------------------


def transfer_view(app: Any, rec: Any, target: str | None = None) -> dict[str, Any]:
    """Экран запуска: в какие скрипты можно передать файлы этого запуска и как их сопоставить.

    targets: скрипты с несекретными table или file; при выбранном `target` rows: параметр и
    предложенный файл (по объявленным выходам, совпадению колонок или главный файл)."""
    files = outputs.output_files(app.paths.run_dir(rec.id) / "output", limit=outputs.MAX_FILES)
    targets = []
    for s in app.registry.list():
        if s.name == rec.script_name:
            continue
        params = eligible_params(app, s.name)
        if params:
            targets.append({"name": s.name, "title": s.title or s.name})
    out: dict[str, Any] = {"run_id": rec.id, "script": rec.script_name, "files": files, "targets": targets,
                           "target": None, "rows": []}
    if not target or target not in {t["name"] for t in targets}:
        return out
    declared: dict[str, Any] = {}
    try:
        for o in app.registry.manifest(rec.script_name).outputs:
            declared[o.name] = o
    except ReplayError:
        pass
    from dawg_replay.runner import last_result
    from dawg_replay.runview import result_extras

    run_dir = app.paths.run_dir(rec.id)
    result = last_result(run_dir / "events.jsonl")
    main = result_extras(result, run_dir / "output")["main"] if result else None

    def columns_of(file: str) -> list[str] | None:
        for o in declared.values():
            if o.matches(file) and o.columns:
                return list(o.columns)
        return None

    used: set[str] = set()
    rows = []
    for prm in eligible_params(app, target):
        pick = None
        if prm["columns"]:
            for f in files:
                cols = columns_of(f)
                if f not in used and cols and set(prm["columns"]) <= set(cols):
                    pick = f
                    break
        if pick is None and main in files and main not in used:
            pick = main
        if pick is None:
            pick = next((f for f in files if f not in used), None)
        if pick:
            used.add(pick)
        rows.append({**prm, "pick": pick})
    out["target"] = {"name": target, "title": _title(app, target)}
    out["rows"] = rows
    return out


def passed_note(app: Any, run_id: str, refs: dict[str, str]) -> dict[str, Any] | None:
    """Форма запуска, открытая кнопкой «Передать»: откуда файлы и можно ли сделать так всегда."""
    try:
        rec = app.runs.get(run_id)
    except ReplayError:
        return None
    files = []
    for param, text in refs.items():
        ref = links.parse_ref(text)
        if ref is not None and ref.run_id == run_id:
            files.append({"param": param, "file": ref.file})
    return {"run_id": run_id, "script": rec.script_name, "title": _title(app, rec.script_name),
            "created_at": rec.created_at, "files": files}
