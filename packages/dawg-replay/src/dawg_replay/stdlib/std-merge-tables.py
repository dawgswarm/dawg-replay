# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "std-merge-tables"
# title = "Склеить таблицы"
# description = "Складывает строки нескольких таблиц в одну по порядку: первая, вторая и дальше. Может добавить колонку, из какой таблицы строка."
# tags = ["Таблицы", "Встроенный"]
# version = 1
# timeout_minutes = 10
#
# [[params]]
# name = "table_1"
# type = "table"
# title = "Таблица 1"
# required = true
# max_rows = 100000
#
# [[params]]
# name = "table_2"
# type = "table"
# title = "Таблица 2"
# max_rows = 100000
#
# [[params]]
# name = "table_3"
# type = "table"
# title = "Таблица 3"
# max_rows = 100000
#
# [[params]]
# name = "table_4"
# type = "table"
# title = "Таблица 4"
# max_rows = 100000
#
# [[params]]
# name = "table_5"
# type = "table"
# title = "Таблица 5"
# max_rows = 100000
#
# [[params]]
# name = "columns"
# type = "enum"
# title = "Если колонки разные"
# default = "all"
# choices = [
#   { value = "all", title = "взять все колонки, недостающие оставить пустыми" },
#   { value = "common", title = "взять только колонки, которые есть во всех таблицах" },
#   { value = "same", title = "остановиться с ошибкой" },
# ]
#
# [[params]]
# name = "source_column"
# type = "string"
# title = "Колонка источника"
# description = "Название новой колонки с именем таблицы, откуда строка. Пусто: колонку не добавлять"
#
# [[params]]
# name = "source_names"
# type = "string_list"
# title = "Имена таблиц"
# description = "Для колонки источника, по порядку таблиц. Без имени: «Таблица 1», «Таблица 2»…"
#
# [[outputs]]
# name = "merged.csv"
# title = "Склеенная таблица"
# main = true
# ///

"""Стандартный узел «Склеить таблицы». Без браузера и ключа DAWG.

Пустые слоты пропускаются. Порядок колонок: как в первой таблице, новые колонки следующих
таблиц дописываются справа. Колонка источника (если задана) идёт первой.
"""

import csv
import sys

import dawg_replay_kit as kit

ROW_KEY = "_row"
SLOTS = ("table_1", "table_2", "table_3", "table_4", "table_5")


def fail(text):
    kit.result("failed", text)
    sys.exit(0)


def text_of(cell):
    return "" if cell is None else str(cell)


def quoted(names):
    return ", ".join(f"«{c}»" for c in names)


def main():
    p = kit.params
    names = [n.strip() for n in (p.source_names or [])]
    tables = []  # (имя, колонки, строки)
    for i, slot in enumerate(SLOTS):
        rows = getattr(p, slot)
        if not rows:
            continue
        name = names[i] if i < len(names) and names[i] else f"Таблица {i + 1}"
        tables.append((name, [k for k in rows[0] if k != ROW_KEY], rows))
    if not tables:
        fail("нет ни одной таблицы")

    first = tables[0][1]
    if p.columns == "same":
        for name, cols, _ in tables[1:]:
            if set(cols) != set(first):
                extra = [c for c in cols if c not in first]
                lack = [c for c in first if c not in cols]
                parts = ([f"лишние {quoted(extra)}"] if extra else []) + ([f"нет {quoted(lack)}"] if lack else [])
                fail(f"колонки «{name}» не совпадают с «{tables[0][0]}»: " + "; ".join(parts))
        columns = list(first)
    elif p.columns == "common":
        columns = [c for c in first if all(c in cols for _, cols, _ in tables[1:])]
        if not columns:
            fail("у таблиц нет ни одной общей колонки")
    else:
        columns = list(first)
        for _, cols, _ in tables[1:]:
            columns += [c for c in cols if c not in columns]

    source = (p.source_column or "").strip()
    if source and source in columns:
        fail(f"колонка источника «{source}» уже есть в таблице, выберите другое название")
    header = ([source] if source else []) + columns

    path = kit.output_dir / "merged.csv"
    total = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for name, _, rows in tables:
            for row in rows:
                w.writerow(([name] if source else []) + [text_of(row.get(c)) for c in columns])
            total += len(rows)
            kit.log.info(f"{name}: строк {len(rows)}")
    kit.artifact(path, "Склеенная таблица", "text/csv")
    kit.result("ok", f"склеено таблиц: {len(tables)}, строк: {total}", main="merged.csv")


main()
