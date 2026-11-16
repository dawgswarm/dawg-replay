"""«Перезапустить упавшие»: новый запуск только со строками таблицы, которые не получились (ТЗ M3, раздел 2).

    p = plan(app, rec)      # None: кнопки нет; иначе что и откуда перезапускать
    run = start(ui_runs, app, rec)

Кнопка есть, когда у скрипта ровно один параметр `table`, а события `item` запуска
содержат `row` (значение `_row` строки таблицы). В новый запуск идут строки, чьи `row`
получили `failed`, и, если запуск оборвался (итог `failed`: упал, отменён, таймаут),
строки, до которых скрипт не дошёл. Строки `ok` и `skipped` не повторяются.

Откуда таблица:

- обычная лежит в параметрах запуска в БД;
- секретная в историю не попадает: берётся из загрузки, по которой запускали
  (`Uploads.link`), пока она жива (сутки). Иначе кнопка просит загрузить файл заново.

Новый запуск идёт без пресета: таблица в нём короче, и детектор тихой поломки не должен
сравнивать его с полными запусками пресета.
"""

from __future__ import annotations

import json
from typing import Any

from dawg_replay import repair
from dawg_replay.errors import ReplayError
from dawg_replay.runview import item_id
from dawg_replay.uploads import Uploads


def _table_param(app: Any, rec: Any) -> Any | None:
    try:
        manifest = app.registry.manifest(rec.script_name)
    except ReplayError:
        return None
    tables = [p for p in manifest.params if p.type == "table"]
    return tables[0] if len(tables) == 1 else None


def _item_rows(run_dir: Any) -> tuple[set[int], set[int]] | None:
    """(упавшие row, сделанные row) по последнему item каждого key; None, если row нигде нет."""
    last: dict[str, dict[str, Any]] = {}
    try:
        f = open(run_dir / "events.jsonl", "rb")
    except OSError:
        return None
    with f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("t") == "item" and e.get("status") in repair.ITEM_STATUSES:
                ident = item_id(e.get("key"), e.get("row"))
                if ident is not None:
                    last[ident] = e
    failed, done = set(), set()
    for e in last.values():
        row = e.get("row")
        if not isinstance(row, int) or isinstance(row, bool):
            continue
        (failed if e["status"] == "failed" else done).add(row)
    if not failed and not done:
        return None
    return failed - done, done


def plan(app: Any, rec: Any) -> dict[str, Any] | None:
    """Что перезапускать. None: кнопки нет (запуск идёт, нет таблицы, нет row в item).
    Иначе {"param", "secret", "failed", "unreached", "table_rows",
    "rows" (None, если таблицу надо загрузить заново)}."""
    if rec.running or rec.draft:
        return None
    p = _table_param(app, rec)
    if p is None:
        return None
    rows_info = _item_rows(app.paths.run_dir(rec.id))
    if rows_info is None:
        return None
    failed, done = rows_info
    broke_off = rec.status == "failed"
    if p.secret:
        data = Uploads(app.paths.uploads).linked(rec.id, p.name)
        table = data["rows"] if data else None
        file = str(data.get("file") or "") if data else ""
    else:
        table = rec.params.get(p.name)
        table = table if isinstance(table, list) else None
        file = ""
    out: dict[str, Any] = {"param": p.name, "secret": p.secret, "file": file, "failed": len(failed),
                           "unreached": 0, "rows": None, "table_rows": len(table) if table is not None else None}
    if table is None:
        # Секретная таблица больше не хранится: число упавших известно из отчёта, строк нет.
        return out if failed or broke_off else None
    picked = []
    for r in table:
        row = r.get("_row") if isinstance(r, dict) else None
        if row in failed:
            picked.append(r)
        elif broke_off and row not in done:
            picked.append(r)
            out["unreached"] += 1
    if not picked:
        return None
    out["rows"] = picked
    return out


def start(runs: Any, app: Any, rec: Any) -> Any:
    """Новый запуск с упавшими строками. `runs`: UiRuns (или то, у чего есть start)."""
    p = plan(app, rec)
    if p is None:
        raise ReplayError("nothing_to_retry", "перезапускать нечего: упавших строк нет")
    if p["rows"] is None:
        raise ReplayError(
            "table_expired",
            "секретная таблица этого запуска уже не хранится (хранится сутки): запустите скрипт заново с файлом",
        )
    manifest = app.registry.manifest(rec.script_name)
    names = {x.name for x in manifest.params}
    values = {k: v for k, v in rec.params.items() if k in names}
    values[p["param"]] = p["rows"]
    run = runs.start(rec.script_name, values=values, inputs_of=rec.id)
    if p["secret"]:
        # Перезапуск перезапуска: у нового запуска тоже есть откуда взять секретную таблицу.
        store = Uploads(app.paths.uploads)
        uid = store.put(p["param"], p["file"] or "таблица", p["rows"], secret=True)
        store.link(run.id, {p["param"]: uid})
    return run
