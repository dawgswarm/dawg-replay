"""Момент прошлого визита в UI для блока «С прошлого визита» на главной.

Хранится в `<каталог данных>/ui-state.json`, не в config.toml: это не настройка,
которую человек меняет, и в `config show` ей делать нечего.

Визит: открытия главной с перерывом не больше VISIT_GAP. Пока визит идёт,
граница `since` не меняется, и блок не пропадает при обновлении страницы.
Новый визит начинается после перерыва: `since` становится моментом, когда
главную открывали в прошлый раз.

    since = visit(path)    # открыли главную: обновить и получить границу
    since = peek(path)     # живое обновление блока: только прочитать
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dawg_replay.db import utcnow
from dawg_replay.runs import parse_ts

VISIT_GAP = timedelta(minutes=30)


def _load(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, str]) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def peek(path: Path) -> str | None:
    return _load(path).get("since")


def visit(path: Path, now: str | None = None) -> str | None:
    """Отмечает открытие главной. Возвращает границу: показывать то, что закончилось позже неё.
    При самом первом открытии None: прошлого визита не было."""
    now = now or utcnow()
    data = _load(path)
    last = parse_ts(data.get("last_seen"))
    current = parse_ts(now) or datetime.now(timezone.utc)
    if last is None:
        data.pop("since", None)
    elif current - last > VISIT_GAP:
        data["since"] = data["last_seen"]
    data["last_seen"] = now
    _save(path, data)
    return data.get("since")
