"""Значения по стрелке проекта: выбор строк таблицы (limit, where), колонка во вход, приведение типа.

Условие `where` считается так же, как в узле «Отфильтровать строки» (stdlib/std-filter-rows.py):
операции equals, not_equals, contains, not_contains, gt, lt, empty, not_empty; для gt и lt
ячейка и значение это числа, пробелы между разрядами и запятая вместо точки допустимы.

    rows = select(table, where, limit)
    value, problem = coerce(param, "12")
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dawg_replay_kit.manifest import Param

    from dawg_replay.projects import Where

ROW_KEY = "_row"
TEXT_TYPES = ("string", "text", "secret", "enum", "url", "date", "time", "datetime")
NUMBER_TYPES = ("int", "float")
LIST_TYPES = ("string_list", "url_list", "multi_enum")
FILE_TYPES = ("table", "file")
TRUE_WORDS = ("true", "1", "да", "yes", "y", "истина", "вкл")
FALSE_WORDS = ("false", "0", "нет", "no", "n", "ложь", "выкл", "")


def number(text: Any) -> float | None:
    """Число из ячейки: пробелы между разрядами и запятая вместо точки допустимы. Не число: None."""
    if isinstance(text, bool) or text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if math.isfinite(text) else None
    s = str(text).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        n = float(s)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def cell_text(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)


def matches(cell: Any, where: Where) -> bool:
    """Подходит ли ячейка под условие. Для gt и lt не число не подходит."""
    text = cell_text(cell).strip()
    op = where.op
    if op == "empty":
        return text == ""
    if op == "not_empty":
        return text != ""
    value = (where.value or "").strip()
    if op in ("gt", "lt"):
        a, b = number(text), number(value)
        if a is None or b is None:
            return False
        return a > b if op == "gt" else a < b
    a, b = (text.casefold(), value.casefold()) if where.ignore_case else (text, value)
    if op == "equals":
        return a == b
    if op == "not_equals":
        return a != b
    if op == "contains":
        return b in a
    return b not in a


def columns(rows: list[dict[str, Any]]) -> list[str]:
    return [k for k in (rows[0] if rows else {}) if not k.startswith("_")]


def select(rows: list[dict[str, Any]], where: Where | None, limit: int | None) -> list[dict[str, Any]]:
    """Строки по условию, затем первые `limit`. Нет колонки условия: ValueError с понятным текстом."""
    if where is not None:
        if rows and where.column not in columns(rows):
            raise ValueError(f"в таблице нет колонки «{where.column}» из условия where; есть: "
                             + ", ".join(f"«{c}»" for c in columns(rows)))
        rows = [r for r in rows if matches(r.get(where.column), where)]
    return rows[:limit] if limit is not None else rows


def column_values(rows: list[dict[str, Any]], column: str) -> list[Any]:
    """Колонка таблицы. Нет колонки: ValueError."""
    if rows and column not in columns(rows):
        raise ValueError(f"в таблице нет колонки «{column}»; есть: " + ", ".join(f"«{c}»" for c in columns(rows)))
    return [r.get(column) for r in rows]


def coerce(p: Param, raw: Any) -> tuple[Any, str | None]:
    """Значение из ячейки или выходного значения в тип входа: (значение, None) или (None, причина).
    Число в текст можно, текст в число, если это число, одно значение в список списком из одного."""
    from dawg_replay_kit.manifest import check_value

    t = p.type
    value: Any = raw
    if t in TEXT_TYPES:
        if isinstance(raw, list):
            return None, f"во вход «{p.title}» нужно одно значение, а пришёл список"
        value = cell_text(raw).strip() if t not in ("text", "secret") else cell_text(raw)
    elif t in NUMBER_TYPES:
        n = number(raw)
        if n is None:
            return None, f"во вход «{p.title}» нужно число, а пришло «{cell_text(raw)}»"
        if t == "int":
            if not n.is_integer():
                return None, f"во вход «{p.title}» нужно целое, а пришло «{cell_text(raw)}»"
            value = int(n)
        else:
            value = n
    elif t == "bool":
        if isinstance(raw, bool):
            value = raw
        else:
            word = cell_text(raw).strip().casefold()
            if word in TRUE_WORDS:
                value = True
            elif word in FALSE_WORDS:
                value = False
            else:
                return None, f"во вход «{p.title}» нужно да или нет, а пришло «{cell_text(raw)}»"
    elif t in LIST_TYPES:
        items = raw if isinstance(raw, list) else [raw]
        value = [cell_text(x).strip() for x in items if cell_text(x).strip() != ""]
    else:
        return None, f"вход «{p.title}» типа {t}: значение по стрелке в него не передаётся"
    problem = check_value(p, value)
    if problem:
        return None, f"«{p.title}»: {problem}"
    return value, None


def compat(out_type: str | None, p: Param) -> str | None:
    """Совместимость типа выходного значения со входом при сохранении: None, "warning" или "error"."""
    t = p.type
    if t in FILE_TYPES:
        return "error"
    if out_type is None or out_type == t:
        return None
    if t in LIST_TYPES:
        return None if out_type in (*LIST_TYPES, *TEXT_TYPES) else "warning"
    if out_type in LIST_TYPES:
        return "error"  # список в одно значение
    if t in TEXT_TYPES:
        return None if t in ("string", "text", "secret") else "warning"
    return "warning"  # текст в число или да/нет: проверка значения при старте шага
