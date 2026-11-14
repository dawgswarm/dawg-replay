"""Почему упал запуск, если скрипт сам ничего не сообщил.

Запуск может упасть до первого события: uv не поставил зависимости, Python не
нашёлся, скрипт упал на импорте. Тогда в events.jsonl нет ни `result`, ни
`log` уровня error, а причина только в stderr.log. Здесь хвост stderr (с
маскировкой известных секретов) и подсказка для частых случаев. Используют
экран запуска, главная и CLI (`run --follow`, `logs`).

    d = diagnose(app, rec)   # None, если запуск не упал или скрипт сам объяснил причину
    d["tail"], d["hint"]
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dawg_replay.errors import ReplayError

TAIL_LINES = 20
TAIL_BYTES = 64 * 1024
MASK = "***"

KIT_NOT_FOUND = "dawg-replay-kit was not found in the package registry"
KIT_HINT = (
    "kit не найден: пока dawg-replay-kit не опубликован, задайте "
    "dawg-replay config set dev_kit_source <путь к packages/dawg-replay-kit>"
)
# Признак в stderr -> подсказка. Порядок важен: первая подошедшая.
HINTS: tuple[tuple[str, str], ...] = ((KIT_NOT_FOUND, KIT_HINT),)


def hint_for(text: str) -> str | None:
    flat = " ".join(text.split())  # uv переносит длинные строки по ширине терминала
    for needle, hint in HINTS:
        if needle in flat:
            return hint
    return None


def read_tail(path: Path, lines: int = TAIL_LINES, max_bytes: int = TAIL_BYTES) -> list[str]:
    """Последние строки файла: читается не больше max_bytes с конца."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    out = [line.rstrip("\r") for line in text.split("\n")]
    if size > max_bytes and out:
        out = out[1:]  # первая строка обрезана посередине
    while out and not out[-1].strip():
        out.pop()
    return out[-lines:]


def mask(text: str, secrets: Iterable[str]) -> str:
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, MASK)
    return text


def explained(run_dir: Path) -> bool:
    """В событиях есть `result` или `log` уровня error: скрипт сам объяснил, что случилось."""
    try:
        with open(run_dir / "events.jsonl", "rb") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict) and (e.get("t") == "result" or (e.get("t") == "log" and e.get("level") == "error")):
                    return True
    except OSError:
        pass
    return False


def known_secrets(app: Any, script: str) -> list[str]:
    """Ключ DAWG и сохранённые секреты скрипта: их не должно быть в показанном stderr."""
    out: list[str] = []
    try:
        key = app.secrets.api_key_raw()
        if key:
            out.append(key)
    except ReplayError:
        pass
    try:
        manifest = app.registry.manifest(script)
    except ReplayError:
        return out
    for p in manifest.params:
        if p.type == "secret":
            try:
                value = app.secrets.get_secret(script, p.name)
            except ReplayError:
                value = None
            if value:
                out.append(value)
    return out


def diagnose(app: Any, rec: Any, lines: int = TAIL_LINES) -> dict[str, Any] | None:
    """Хвост stderr и подсказка для упавшего запуска, который сам ничего не объяснил."""
    if rec.running or rec.status != "failed" or rec.reason == "cancelled":
        return None
    run_dir = app.paths.run_dir(rec.id)
    if explained(run_dir):
        return None
    tail = read_tail(run_dir / "stderr.log", lines)
    if not tail:
        return None
    secrets = known_secrets(app, rec.script_name)
    tail = [mask(line, secrets) for line in tail]
    text = "\n".join(tail)
    return {"tail": tail, "hint": hint_for(text), "summary": hint_for(text) or _last_meaningful(tail)}


def _last_meaningful(tail: list[str]) -> str:
    """Короткая причина для списка: последняя непустая строка, не длиннее 200 символов."""
    for line in reversed(tail):
        s = line.strip()
        if s and not s.startswith(("hint:", "help:")):
            return s[:200]
    return tail[-1].strip()[:200]
