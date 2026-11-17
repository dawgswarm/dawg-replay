"""Выходные значения запусков (контракт 1.5): то, что скрипт отдал вызовом `kit.output`.

Kit пишет значения в `values.json` каталога запуска (`DAWG_SCRIPT_VALUES`). После завершения
`collect` переносит их в таблицу `run_values` и удаляет файл. Секретное значение в БД не
хранится (`value_json` NULL), оно уходит в хранилище паролей под ключом `run:<id>:<имя>`.
Нет хранилища паролей: секрет не сохраняется, в журнале запуска предупреждение.

    run_values.read(app, run_id)     # {имя: значение}, секретные тоже: для передачи в шаг
    run_values.shown(app, run_id)    # для экрана и CLI, секретные без значения
    run_values.declared(manifest)    # объявленные значения скрипта
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay_kit.manifest import (
    Manifest,
    ManifestError,
    Output,
    Param,
    load_manifest,
)

from dawg_replay.errors import ReplayError
from dawg_replay.runner import VALUES_FILE

if TYPE_CHECKING:
    from dawg_replay.app import App

CHECKED_STATUSES = ("ok", "partial")  # у них необъявленное обязательное значение это тихая поломка
SOURCE = "dawg-replay-values"  # source строк журнала о приёме значений


def declared(manifest: Manifest | None) -> list[Output]:
    """Объявленные выходные значения скрипта (Output.kind == "value")."""
    return list(manifest.values_out()) if manifest is not None else []


def coerce(p: Param, value: Any) -> Any:
    """Значение для параметра другого типа: число и да/нет в текст строкой, список в список строк."""
    if p.type in ("string", "text", "url", "enum") and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if p.type in ("string", "text") and isinstance(value, bool):
        return "да" if value else "нет"
    if p.type in ("string_list", "url_list") and isinstance(value, str):
        return [value]
    if p.type == "float" and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def _journal(run_dir: Path, level: str, msg: str, **extra: Any) -> None:
    if not run_dir.is_dir():
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    e = {"t": "log", "ts": ts, "level": level, "msg": msg, "source": SOURCE, **extra}
    try:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"": None}  # битый файл: одна строка в журнале ниже
    return data if isinstance(data, dict) else {"": None}


def collect(app: App, run_id: str) -> list[str]:
    """Забирает значения закончившегося запуска из values.json в БД (секреты в хранилище паролей)
    и удаляет файл. Повторный вызов ничего не меняет. Для `ok` и `partial` сверяет с объявленными:
    необъявленное обязательное значение даёт строку журнала и тихую поломку (dawg_replay.repair).
    Возвращает имена недостающих обязательных значений."""
    from dawg_replay import repair

    try:
        rec = app.runs.get(run_id)
    except ReplayError:
        return []
    if rec.running:
        return []
    run_dir = app.paths.run_dir(run_id)
    path = run_dir / VALUES_FILE
    try:
        manifest = load_manifest(rec.script_path)
    except (ManifestError, OSError):
        path.unlink(missing_ok=True)
        return []
    outs = {o.name: o for o in declared(manifest)}
    got = _read_file(path)
    if got.get("") is None and "" in got:
        _journal(run_dir, "warn", "Файл выходных значений повреждён, значения не приняты.")
        got = {}
    for name, value in got.items():
        o = outs.get(name)
        if o is None:
            _journal(run_dir, "warn", f"Значение `{name}` не объявлено в outputs, не принято.")
            continue
        problem = o.check(value)
        if problem:
            _journal(run_dir, "warn", f"Значение `{name}` не принято: {problem}.")
            continue
        _store(app, run_dir, run_id, o, value)
    path.unlink(missing_ok=True)

    if rec.status not in CHECKED_STATUSES or rec.draft:
        return []
    have = {r["name"] for r in app.db.execute("SELECT name FROM run_values WHERE run_id = ?", (run_id,))}
    lost = [o for o in outs.values() if not o.optional and o.name not in have]
    already = set(repair.scan(run_dir).missing)
    for o in lost:
        if f"={o.name}" not in already:
            what = f"«{o.title}» ({o.name})" if o.title else o.name
            _journal_missing(run_dir, o, f"Нет значения {what}, которое скрипт объявил в outputs.")
    return [o.name for o in lost]


def _journal_missing(run_dir: Path, o: Output, msg: str) -> None:
    from dawg_replay import repair

    if not run_dir.is_dir():
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    # Тот же source, что у пропавшего файла: repair считает это тихой поломкой. `=` отличает значение.
    e = {"t": "log", "ts": ts, "level": "warn", "msg": msg, "source": repair.MISSING_SOURCE, "output": f"={o.name}"}
    try:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _store(app: App, run_dir: Path, run_id: str, o: Output, value: Any) -> None:
    value_json: str | None = json.dumps(value, ensure_ascii=False)
    if o.secret:
        try:
            app.secrets.set_run_value(run_id, o.name, value_json)
        except Exception:  # noqa: BLE001 - нет хранилища паролей (fail-бэкенд, Linux без Secret Service)
            _journal(run_dir, "warn", f"Секретное значение {o.name} не сохранено: нет хранилища секретов.")
            return
        value_json = None
    app.db.execute(
        "INSERT INTO run_values (run_id, name, type, secret, value_json) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (run_id, name) DO UPDATE SET type = excluded.type, secret = excluded.secret,"
        " value_json = excluded.value_json",
        (run_id, o.name, o.type, int(o.secret), value_json),
    )


def read(app: App, run_id: str) -> dict[str, Any]:
    """Все принятые значения запуска, секретные тоже. Секрет, которого нет в хранилище паролей
    (или хранилище недоступно), не отдаётся."""
    out: dict[str, Any] = {}
    for r in app.db.execute("SELECT * FROM run_values WHERE run_id = ? ORDER BY rowid", (run_id,)):
        raw = r["value_json"]
        if r["secret"]:
            try:
                raw = app.secrets.get_run_value(run_id, r["name"])
            except Exception:  # noqa: BLE001 - хранилище паролей недоступно
                raw = None
        if raw is None:
            continue
        try:
            out[r["name"]] = json.loads(raw)
        except ValueError:
            continue
    return out


def shown(app: App, run_id: str) -> list[dict[str, Any]]:
    """Значения для экрана запуска и CLI: name, title, type, secret, value (у секретного None)."""
    titles: dict[str, str | None] = {}
    try:
        rec = app.runs.get(run_id)
        titles = {o.name: o.title for o in declared(load_manifest(rec.script_path))}
    except (ReplayError, ManifestError, OSError):
        pass
    items = []
    for r in app.db.execute("SELECT * FROM run_values WHERE run_id = ? ORDER BY rowid", (run_id,)):
        value = None
        if not r["secret"] and r["value_json"] is not None:
            try:
                value = json.loads(r["value_json"])
            except ValueError:
                value = None
        items.append({"name": r["name"], "title": titles.get(r["name"]), "type": r["type"],
                      "secret": bool(r["secret"]), "value": value})
    return items


def forget(app: App, run_id: str) -> None:
    """Удаляет значения запуска и его секреты из хранилища паролей (для чистки истории)."""
    for r in app.db.execute("SELECT name FROM run_values WHERE run_id = ? AND secret = 1", (run_id,)).fetchall():
        try:
            app.secrets.delete_run_value(run_id, r["name"])
        except Exception:  # noqa: BLE001, S110 - хранилища нет: секрет туда и не попал
            pass
    app.db.execute("DELETE FROM run_values WHERE run_id = ?", (run_id,))
    (app.paths.run_dir(run_id) / VALUES_FILE).unlink(missing_ok=True)
