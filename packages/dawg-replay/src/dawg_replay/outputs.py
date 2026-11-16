"""Выходы скрипта: объявленные в манифесте (контракт 1.4, `[[outputs]]`) и реальные файлы запуска.

    check_run(app, run_id)      # после запуска: каких объявленных файлов нет, строка в журнал
    script_outputs(app, name)   # что скрипт отдаёт, для настроек связи между скриптами

Недостающий файл попадает в журнал запуска строкой с полем `output`, оттуда его читает
детектор тихой поломки (repair.suspicion, вид `missing_output`).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay_kit.manifest import ManifestError, Output, load_manifest

from dawg_replay import repair
from dawg_replay.errors import ReplayError
from dawg_replay.runner import last_result
from dawg_replay.runview import result_extras

if TYPE_CHECKING:
    from dawg_replay.app import App

CHECKED_STATUSES = ("ok", "partial")  # упавший запуск и не обязан был всё сохранить
MAX_FILES = 200  # сколько файлов последнего запуска предлагать
SCAN_LIMIT = 10000  # сколько файлов каталога результата просматривать
RUNS_SCAN = 50  # среди скольких последних запусков искать подходящий


def output_files(output_dir: Path, limit: int = SCAN_LIMIT) -> list[str]:
    """Обычные файлы каталога результата, пути через `/`, по алфавиту. Ссылки не идут."""
    found: list[str] = []
    if not output_dir.is_dir():
        return found
    for root, dirs, files in os.walk(output_dir):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
        for name in sorted(files):
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            found.append(Path(full).relative_to(output_dir).as_posix())
            if len(found) >= limit:
                return found
    return found


def missing(declared: tuple[Output, ...], files: list[str]) -> list[Output]:
    """Объявленные выходы, которым не нашлось ни одного файла."""
    return [o for o in declared if not any(o.matches(f) for f in files)]


def check_run(app: App, run_id: str) -> list[str]:
    """Сверяет объявленные выходы с файлами закончившегося запуска (`ok` и `partial`).
    На каждый недостающий файл одна строка в журнале запуска; повторный вызов строк не дублирует.
    Возвращает имена недостающих выходов."""
    try:
        rec = app.runs.get(run_id)
    except ReplayError:
        return []
    if rec.running or rec.status not in CHECKED_STATUSES:
        return []
    try:
        declared = load_manifest(rec.script_path).outputs
    except ManifestError:
        return []
    if not declared:
        return []
    run_dir = app.paths.run_dir(run_id)
    lost = missing(declared, output_files(run_dir / "output"))
    already = set(repair.scan(run_dir).missing)
    for o in lost:
        if o.name not in already:
            _log(run_dir, o)
    return [o.name for o in lost]


def _log(run_dir: Path, o: Output) -> None:
    what = f"«{o.title}» ({o.name})" if o.title else o.name
    kind = "по шаблону " if o.is_pattern else ""
    msg = f"Нет файла {kind}{what}, который скрипт объявил в outputs."
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    e = {"t": "log", "ts": ts, "level": "warn", "msg": msg, "source": repair.MISSING_SOURCE, "output": o.name}
    if not run_dir.is_dir():
        return
    try:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


def script_outputs(app: App, name: str) -> dict[str, Any]:
    """Что отдаёт скрипт, для настроек связи.

    {"source": "declared" | "last_run" | "none", "run_id": id запуска или None,
     "outputs": [{"name", "title", "columns", "main", "pattern"}]}

    Объявленные выходы текущей версии идут первыми. Без объявления (или если файл скрипта
    не читается) берутся файлы последнего запуска `ok` или `partial`, главный файл из `main` итога.
    Скрипт не зарегистрирован: ReplayError `script_not_found`.
    """
    app.registry.get(name)
    try:
        declared = app.registry.manifest(name).outputs
    except ReplayError:
        declared = ()
    if declared:
        return {"source": "declared", "run_id": None,
                "outputs": [_declared(o) for o in declared]}
    for rec in app.runs.list(name, RUNS_SCAN):
        if rec.draft or rec.running or rec.status not in CHECKED_STATUSES:
            continue
        run_dir = app.paths.run_dir(rec.id)
        files = output_files(run_dir / "output")
        if not files:
            continue
        result = last_result(run_dir / "events.jsonl")
        main = result_extras(result, run_dir / "output")["main"] if result else None
        files = files[:MAX_FILES]
        if main in files:  # главный файл первым
            files.remove(main)
            files.insert(0, main)
        return {"source": "last_run", "run_id": rec.id,
                "outputs": [{"name": f, "title": None, "columns": None, "main": f == main, "pattern": False}
                            for f in files]}
    return {"source": "none", "run_id": None, "outputs": []}


def _declared(o: Output) -> dict[str, Any]:
    return {"name": o.name, "title": o.title, "columns": list(o.columns) if o.columns is not None else None,
            "main": o.main, "pattern": o.is_pattern}
