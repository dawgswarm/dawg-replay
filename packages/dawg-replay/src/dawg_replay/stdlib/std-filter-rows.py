# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "std-filter-rows"
# title = "Отфильтровать строки"
# description = "Оставляет строки таблицы, где колонка подходит под условие. Если ничего не осталось, файла нет: по этому можно ветвить проект."
# tags = ["Таблицы", "Встроенный"]
# version = 1
# timeout_minutes = 10
#
# [[params]]
# name = "table"
# type = "table"
# title = "Таблица"
# required = true
# max_rows = 100000
#
# [[params]]
# name = "column"
# type = "string"
# title = "Колонка"
# description = "Название колонки как в шапке таблицы"
# required = true
#
# [[params]]
# name = "op"
# type = "enum"
# title = "Условие"
# default = "equals"
# choices = [
#   { value = "equals", title = "равно" },
#   { value = "not_equals", title = "не равно" },
#   { value = "contains", title = "содержит" },
#   { value = "not_contains", title = "не содержит" },
#   { value = "gt", title = "больше" },
#   { value = "lt", title = "меньше" },
#   { value = "empty", title = "пусто" },
#   { value = "not_empty", title = "не пусто" },
# ]
#
# [[params]]
# name = "value"
# type = "string"
# title = "Значение"
# description = "С чем сравнивать. Для «больше» и «меньше» число, для «пусто» и «не пусто» не нужно"
#
# [[params]]
# name = "ignore_case"
# type = "bool"
# title = "Без учёта регистра"
# default = true
#
# [[outputs]]
# name = "rows.csv"
# title = "Подходящие строки"
# main = true
# optional = true
# ///

"""Стандартный узел «Отфильтровать строки». Без браузера и ключа DAWG.

Подходящие строки идут в rows.csv в исходном порядке и с исходными колонками.
Если ни одна строка не подошла, rows.csv не создаётся: стрелка проекта с `if_file = "rows.csv"`
дальше не пойдёт.
"""

import csv
import sys

import dawg_replay_kit as kit

ROW_KEY = "_row"
NEEDS_VALUE = ("equals", "not_equals", "contains", "not_contains", "gt", "lt")
OP_WORDS = {
    "equals": "равно", "not_equals": "не равно", "contains": "содержит", "not_contains": "не содержит",
    "gt": "больше", "lt": "меньше", "empty": "пусто", "not_empty": "не пусто",
}


def fail(text):
    kit.result("failed", text)
    sys.exit(0)


def number(text):
    """Число из ячейки: пробелы между разрядами и запятая вместо точки допустимы. Не число: None."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def text_of(cell):
    return "" if cell is None else str(cell)


def matches(cell, op, value, ignore_case, limit):
    """Подходит ли ячейка; None, если для «больше» и «меньше» в ячейке не число."""
    cell_text = text_of(cell).strip()
    if op == "empty":
        return cell_text == ""
    if op == "not_empty":
        return cell_text != ""
    if op in ("gt", "lt"):
        n = number(cell_text)
        if n is None:
            return None
        return n > limit if op == "gt" else n < limit
    a, b = (cell_text.casefold(), value.casefold()) if ignore_case else (cell_text, value)
    if op == "equals":
        return a == b
    if op == "not_equals":
        return a != b
    if op == "contains":
        return b in a
    return b not in a


def main():
    p = kit.params
    rows = p.table or []
    column, op = p.column.strip(), p.op
    value = (p.value or "").strip()
    columns = [k for k in rows[0] if k != ROW_KEY] if rows else []
    if column not in columns:
        fail(f"в таблице нет колонки «{column}»; есть: " + ", ".join(f"«{c}»" for c in columns))
    if op in NEEDS_VALUE and value == "":
        fail(f"для условия «{OP_WORDS[op]}» нужно значение")
    limit = None
    if op in ("gt", "lt"):
        limit = number(value)
        if limit is None:
            fail(f"для условия «{OP_WORDS[op]}» значение должно быть числом, а не «{value}»")

    kept, not_numbers = [], 0
    for row in rows:
        ok = matches(row.get(column), op, value, p.ignore_case, limit)
        if ok is None:
            not_numbers += 1
        elif ok:
            kept.append(row)
    if not_numbers:
        kit.log.warn(f"в колонке «{column}» не число у строк: {not_numbers}, они не подошли")

    condition = f"«{column}» {OP_WORDS[op]}" + (f" «{value}»" if op in NEEDS_VALUE else "")
    kit.log.info(f"условие {condition}: подошло {len(kept)} из {len(rows)}")
    if not kept:
        # count не передаётся: пустой итог фильтра это исход, а не тихая поломка.
        kit.result("ok", f"ни одна строка не подошла под условие {condition}, файла нет")
        return
    path = kit.output_dir / "rows.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for row in kept:
            w.writerow([text_of(row.get(c)) for c in columns])
    kit.artifact(path, "Подходящие строки", "text/csv")
    kit.result("ok", f"подошло строк: {len(kept)} из {len(rows)}", main="rows.csv")


main()
