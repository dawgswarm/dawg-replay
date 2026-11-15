"""Разбор значений параметров из `--params p.json`, `--set k=v` и `--row таблица k=v ...`.

Общий для пресетов и запуска. Проверка значений идёт через check_value из kit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from dawg_replay_kit.manifest import LIST_TYPES, Manifest, Param, check_value
from dawg_replay_kit.tables import TableError, read_table

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
    if t in ("string", "text", "secret", "enum", "url", "date"):
        return raw
    if t == "table":
        return _table_value(raw)
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


def _table_value(raw: str) -> list[dict[str, Any]]:
    """`@путь` к CSV или XLSX; `[...]` готовая таблица в JSON (как в --params)."""
    if raw.startswith("@"):
        try:
            return read_table(Path(raw[1:]).expanduser())
        except TableError as e:
            raise ValueError(str(e)) from None
    if raw.lstrip().startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"таблица в JSON не разобрана: {e}") from None
    raise ValueError("таблица задаётся файлом: имя=@путь/к/файлу.xlsx или .csv")


def table_files(manifest: Manifest, items: Iterable[str]) -> dict[str, str]:
    """Имена файлов таблиц из `--set имя=@файл`: пресет показывает, из какого файла таблица."""
    tables = {p.name for p in manifest.params if p.type == "table"}
    out: dict[str, str] = {}
    for item in items:
        name, sep, raw = item.partition("=")
        name = name.strip().replace("-", "_")
        if sep and name in tables and raw.startswith("@"):
            out[name] = Path(raw[1:]).name
    return out


HIDDEN = "-"  # значение ячейки в --row: прочитать без отображения


def parse_row_args(
    manifest: Manifest,
    rows: Sequence[Sequence[str]],
    read_hidden: Callable[[str], str],
    sets: Iterable[str] = (),
    warn: Callable[[str], None] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """`--row accounts email=a@b.ru password=-` (повторяемый) в таблицы параметров.

    Строка это словарь ячеек плюс `_row`: номера 2, 3, … как у файла, где шапка строка 1.
    Значение `-` читается через `read_hidden(подсказка)`: с терминала без эха, иначе из stdin.
    Таблицу задают либо строками, либо файлом (`--set имя=@файл`), не тем и другим сразу.
    `warn`: значения секретной таблицы, набранные прямо в команде, видны в истории терминала."""
    tables = {p.name: p for p in manifest.params if p.type == "table"}
    from_set = {item.partition("=")[0].strip().replace("-", "_") for item in sets}
    out: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    visible_secrets: dict[str, list[str]] = {}  # таблица -> колонки, набранные в команде открыто
    for row in rows:
        if not row:
            continue
        name = row[0].strip().replace("-", "_")
        if name not in tables:
            known = ", ".join(tables) or "нет"
            problems.append(f"--row {row[0]}: такой таблицы у скрипта нет (таблицы: {known})")
            continue
        if name in from_set:
            problems.append(f"`{name}`: таблица задана и файлом (--set), и строками (--row); выберите что-то одно")
            continue
        cells: dict[str, Any] = {}
        for token in row[1:]:
            col, sep, value = token.partition("=")
            col = col.strip()
            if not sep or not col:
                problems.append(f"--row {name}: ячейка задаётся как колонка=значение, получено «{token}»")
                continue
            if col.startswith("_"):
                problems.append(f"--row {name}: колонка не может начинаться с «_» ({col})")
                continue
            if value == HIDDEN:
                value = read_hidden(f"{name}, строка {len(out.get(name, [])) + 2}, {col}: ")
            elif tables[name].secret and value and col not in visible_secrets.get(name, []):
                visible_secrets.setdefault(name, []).append(col)
            cells[col] = value
        if not cells:
            problems.append(f"--row {name}: в строке нет ни одной ячейки")
            continue
        out.setdefault(name, []).append({**cells, "_row": len(out.get(name, [])) + 2})
    if visible_secrets and warn is not None:
        for name in sorted(visible_secrets):
            cols = visible_secrets[name]
            which = (f"значение колонки «{cols[0]}» набрано в команде и останется" if len(cols) == 1
                     else "значения колонок " + ", ".join(f"«{c}»" for c in cols) + " набраны в команде и останутся")
            hint = " ".join(f"{c}=-" for c in cols)
            warn(f"таблица `{name}` секретная, а {which} в истории терминала; "
                 f"чтобы ввести без отображения, задайте {hint}")
    if problems:
        raise ReplayError("bad_params", "строки таблиц не подходят:\n" + "\n".join(f"  {x}" for x in problems), problems)
    return out


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
        elif p.is_secret and not allow_secrets:
            problems.append(f"`{name}`: секрет здесь не хранится, задайте его командой `dawg-replay secret set`")
        else:
            problem = check_value(p, value)
            if problem:
                problems.append(f"`{name}`: {problem}")
    return problems


def filter_known(manifest: Manifest, values: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Оставляет значения, которые подходят манифесту. Возвращает (подходящие, отброшенные имена)."""
    kept, why = filter_explained(manifest, values)
    return kept, list(why)


def filter_explained(manifest: Manifest, values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Как filter_known, но для отброшенных ещё и причина для человека:
    {"since": "«С даты»: дата 2026-11-10 раньше min = 2026-11-12"}."""
    kept: dict[str, Any] = {}
    why: dict[str, str] = {}
    by_name = {p.name: p for p in manifest.params}
    for name, value in values.items():
        p = by_name.get(name)
        if p is None:
            why[name] = f"`{name}`: такого параметра в текущей версии скрипта нет"
        elif p.is_secret:
            why[name] = f"«{p.title}»: секреты в пресете не хранятся"
        elif (problem := check_value(p, value)) is not None:
            why[name] = f"«{p.title}»: {problem}"
        else:
            kept[name] = value
    return kept, why


def brief(values: Mapping[str, Any], files: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Значения для показа человеку: таблица заменена строкой «файл, N строк», остальное как есть."""
    files = files or {}
    out: dict[str, Any] = {}
    for k, v in values.items():
        if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
            out[k] = (f"{files[k]}, " if k in files else "таблица, ") + rows_word(len(v))
        else:
            out[k] = v
    return out


def rows_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} строка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} строки"
    return f"{n} строк"
