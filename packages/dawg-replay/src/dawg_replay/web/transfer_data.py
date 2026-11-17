"""Экран запуска: откуда пришли файлы («Запущен после», «из результата …»), куда они ушли («Передал в»),
и «Передать в другой скрипт» с формой запуска получателя.

Только чтение ядра (dawg_replay.transfer, outputs, runs); шаблоны получают готовые словари.
"""

from __future__ import annotations

from typing import Any

from dawg_replay import outputs, transfer
from dawg_replay.errors import ReplayError


def _title(app: Any, name: str) -> str:
    try:
        return app.registry.get(name).title or name
    except ReplayError:
        return name


def eligible_params(app: Any, name: str) -> list[dict[str, Any]]:
    """Параметры получателя, в которые можно передать файл: несекретные table и file."""
    try:
        m = app.registry.manifest(name)
    except ReplayError:
        return []
    out = []
    for p in m.params:
        if p.type in transfer.TRANSFER_TYPES and not p.secret:
            kind = "таблица" if p.type == "table" else ("файлы" if p.multiple else "файл")
            out.append({"name": p.name, "title": p.title, "type": p.type, "kind": kind,
                        "columns": list(p.columns) if p.columns else None})
    return out


def provenance(app: Any, rec: Any) -> dict[str, Any]:
    """Экран запуска: что его запустило и откуда файлы («Запущен после»), куда он передал («Передал в»)."""
    after = None
    if rec.source_run:
        try:
            src = app.runs.get(rec.source_run)
        except ReplayError:
            src = None
        script = src.script_name if src else None
        after = {"script": script, "title": _title(app, script) if script else None,
                 "run_id": rec.source_run, "created_at": src.created_at if src else None}
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
        for o in app.registry.manifest(rec.script_name).files_out():
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
    """Форма запуска, открытая кнопкой «Передать»: откуда файлы."""
    try:
        rec = app.runs.get(run_id)
    except ReplayError:
        return None
    files = []
    for param, text in refs.items():
        ref = transfer.parse_ref(text)
        if ref is not None and ref.run_id == run_id:
            files.append({"param": param, "file": ref.file})
    return {"run_id": run_id, "script": rec.script_name, "title": _title(app, rec.script_name),
            "created_at": rec.created_at, "files": files}
