# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "std-if"
# title = "Если"
# description = "Проверяет условие на таблицу и отдаёт её дальше как yes.csv или no.csv. Стрелки проекта с if_file ведут каждая по своей ветке."
# tags = ["Таблицы", "Встроенный"]
# version = 1
# timeout_minutes = 10
#
# [[params]]
# name = "table"
# type = "table"
# title = "Таблица"
# description = "Без таблицы считается, что строк 0"
# max_rows = 100000
#
# [[params]]
# name = "check"
# type = "enum"
# title = "Проверка"
# default = "rows_gt"
# choices = [
#   { value = "rows_gt", title = "строк больше N" },
#   { value = "any_row", title = "есть строка, где колонка подходит под условие" },
#   { value = "all_rows", title = "во всех строках колонка подходит под условие" },
# ]
#
# [[params]]
# name = "rows"
# type = "int"
# title = "N строк"
# description = "Для проверки «строк больше N»"
# default = 0
# min = 0
#
# [[params]]
# name = "column"
# type = "string"
# title = "Колонка"
# description = "Для проверок по строкам: название колонки как в шапке"
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
# name = "yes.csv"
# title = "Да: таблица целиком"
# optional = true
#
# [[outputs]]
# name = "no.csv"
# title = "Нет: таблица целиком"
# optional = true
# ///

"""Стандартный узел «Если». Без браузера и ключа DAWG.

Отдаёт ровно один файл: yes.csv, если условие выполнено, иначе no.csv. В файле вся входная
таблица (без таблицы файл пустой), чтобы следующий узел мог работать с теми же строками.
В проекте стрелки дальше ставятся с `if_file = "yes.csv"` и `if_file = "no.csv"`.
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
    """Подходит ли ячейка. Для «больше» и «меньше» ячейка не число не подходит."""
    cell_text = text_of(cell).strip()
    if op == "empty":
        return cell_text == ""
    if op == "not_empty":
        return cell_text != ""
    if op in ("gt", "lt"):
        n = number(cell_text)
        if n is None:
            return False
        return n > limit if op == "gt" else n < limit
    a, b = (cell_text.casefold(), value.casefold()) if ignore_case else (cell_text, value)
    if op == "equals":
        return a == b
    if op == "not_equals":
        return a != b
    if op == "contains":
        return b in a
    return b not in a


def decide(rows, columns):
    """(ответ, текст условия)."""
    p = kit.params
    if p.check == "rows_gt":
        return len(rows) > p.rows, f"строк больше {p.rows} (строк {len(rows)})"
    column, op = (p.column or "").strip(), p.op
    value = (p.value or "").strip()
    if not column:
        fail("для проверки по строкам нужна колонка")
    if rows and column not in columns:
        fail(f"в таблице нет колонки «{column}»; есть: " + ", ".join(f"«{c}»" for c in columns))
    if op in NEEDS_VALUE and value == "":
        fail(f"для условия «{OP_WORDS[op]}» нужно значение")
    limit = None
    if op in ("gt", "lt"):
        limit = number(value)
        if limit is None:
            fail(f"для условия «{OP_WORDS[op]}» значение должно быть числом, а не «{value}»")
    condition = f"«{column}» {OP_WORDS[op]}" + (f" «{value}»" if op in NEEDS_VALUE else "")
    hits = sum(1 for r in rows if matches(r.get(column), op, value, p.ignore_case, limit))
    if p.check == "any_row":
        return hits > 0, f"есть строка, где {condition} (таких {hits})"
    # «Во всех строках» у пустой таблицы не выполняется: проверять нечего.
    return bool(rows) and hits == len(rows), f"во всех строках {condition} (подошло {hits} из {len(rows)})"


def main():
    rows = kit.params.table or []
    columns = [k for k in rows[0] if k != ROW_KEY] if rows else []
    answer, condition = decide(rows, columns)
    name = "yes.csv" if answer else "no.csv"
    path = kit.output_dir / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        if columns:
            w = csv.writer(f)
            w.writerow(columns)
            for row in rows:
                w.writerow([text_of(row.get(c)) for c in columns])
    word = "да" if answer else "нет"
    kit.log.info(f"проверка: {condition}; ответ {word}")
    kit.artifact(path, f"Ответ «{word}»: таблица целиком", "text/csv")
    kit.result("ok", f"{word}: {condition}", main=name)


main()
