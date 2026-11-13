"""Разбор значений параметров из `--params p.json` и `--set k=v`.

Общий для пресетов и запуска. Проверка значений идёт через check_value из kit.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from dawg_replay_kit.manifest import LIST_TYPES, Manifest, Param, check_value

from dawg_replay.errors import ReplayError


def read_params_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ReplayError("file_not_found", f"файл параметров не найден: {p}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ReplayError("file_unreadable", f"не удалось прочитать {p}: {e}") from None
    except json.JSONDecodeError as e:
        raise ReplayError("bad_params", f"{p} не JSON: {e}") from None
    if not isinstance(data, dict):
        raise ReplayError("bad_params", f"{p}: ожидается JSON-объект")
    return data


def _convert(p: Param, raw: str) -> Any:
    """Строка из `--set` в значение нужного типа."""
    t = p.type
    if t in ("string", "text", "secret", "enum", "url"):
        return raw
    if t == "bool":
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "да"):
            return True
        if low in ("false", "0", "no", "нет"):
            return False
        raise ValueError(f"ожидается true или false, получено {raw!r}")
    if t == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"ожидается целое, получено {raw!r}") from None
    if t == "float":
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"ожидается число, получено {raw!r}") from None
    if t in LIST_TYPES:
        if raw.lstrip().startswith("["):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"список в JSON не разобран: {e}") from None
            return value
        return [raw]
    raise ValueError(f"неизвестный тип {t}")


def parse_set_args(manifest: Manifest, items: Iterable[str]) -> dict[str, Any]:
    """`k=v` в словарь. Для списков повтор `--set k=v` добавляет элемент, `k=[...]` задаёт JSON-список."""
    out: dict[str, Any] = {}
    problems: list[str] = []
    by_name = {p.name: p for p in manifest.params}
    for item in items:
        if "=" not in item:
            problems.append(f"`{item}`: нужно имя=значение")
            continue
        name, raw = item.split("=", 1)
        name = name.strip().replace("-", "_")
        p = by_name.get(name)
        if p is None:
            problems.append(f"`{name}`: такого параметра нет в манифесте")
            continue
        try:
            value = _convert(p, raw)
        except ValueError as e:
            problems.append(f"`{name}`: {e}")
            continue
        if p.type in LIST_TYPES and isinstance(out.get(name), list) and isinstance(value, list):
            out[name] = out[name] + value
        else:
            out[name] = value
    if problems:
        raise ReplayError("bad_params", "значения параметров не подходят:\n" + "\n".join(f"  {x}" for x in problems), problems)
    return out


def check_partial(manifest: Manifest, values: Mapping[str, Any], allow_secrets: bool) -> list[str]:
    """Проверяет заданные значения без требования обязательных. Возвращает список проблем."""
    problems: list[str] = []
    by_name = {p.name: p for p in manifest.params}
    for name, value in values.items():
        p = by_name.get(name)
        if p is None:
            problems.append(f"`{name}`: такого параметра нет в манифесте")
        elif p.type == "secret" and not allow_secrets:
            problems.append(f"`{name}`: секрет здесь не хранится, задайте его командой `dawg-replay secret set`")
        else:
            problem = check_value(p, value)
            if problem:
                problems.append(f"`{name}`: {problem}")
    return problems


def filter_known(manifest: Manifest, values: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Оставляет значения, которые подходят манифесту. Возвращает (подходящие, отброшенные имена)."""
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    by_name = {p.name: p for p in manifest.params}
    for name, value in values.items():
        p = by_name.get(name)
        if p is None or p.type == "secret" or check_value(p, value) is not None:
            dropped.append(name)
        else:
            kept[name] = value
    return kept, dropped
