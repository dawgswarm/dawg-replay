# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "std-select-columns"
# title = "Выбрать и переименовать колонки"
# description = "Оставляет нужные колонки в нужном порядке и по желанию даёт им новые названия: «Цена = price»."
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
# name = "columns"
# type = "string_list"
# title = "Колонки"
# description = "По одной на строку, в нужном порядке. Переименовать: «Старое название = новое»"
# required = true
#
# [[params]]
# name = "missing"
# type = "enum"
# title = "Если колонки нет"
# default = "fail"
# choices = [
#   { value = "fail", title = "остановиться с ошибкой" },
#   { value = "empty", title = "добавить пустую" },
#   { value = "skip", title = "пропустить" },
# ]
#
# [[outputs]]
# name = "table.csv"
# title = "Таблица с выбранными колонками"
# main = true
# ///

"""Стандартный узел «Выбрать и переименовать колонки». Без браузера и ключа DAWG.

Строка списка: «Колонка» (оставить как есть) или «Колонка = Новое название». Знак `=`
отделяет новое название по последнему вхождению, так что `=` в старом названии допустим.
"""

import csv
import sys

import dawg_replay_kit as kit

ROW_KEY = "_row"


def fail(text):
    kit.result("failed", text)
    sys.exit(0)


def text_of(cell):
    return "" if cell is None else str(cell)


def parse(items):
    """[(колонка, новое название)]."""
    out = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            old, new = (part.strip() for part in item.rsplit("=", 1))
            if not old or not new:
                fail(f"строка «{item}»: нужно «Старое название = новое»")
        else:
            old = new = item
        out.append((old, new))
    if not out:
        fail("не указано ни одной колонки")
    return out


def main():
    p = kit.params
    rows = p.table
    have = [k for k in rows[0] if k != ROW_KEY] if rows else []
    pairs = parse(p.columns)
    lack = [old for old, _ in pairs if old not in have]
    if lack and p.missing == "fail":
        fail("в таблице нет колонок: " + ", ".join(f"«{c}»" for c in lack)
             + "; есть: " + ", ".join(f"«{c}»" for c in have))
    if p.missing == "skip":
        pairs = [(old, new) for old, new in pairs if old in have]
        if not pairs:
            fail("ни одной из указанных колонок в таблице нет")
    header = [new for _, new in pairs]
    dup = sorted({h for h in header if header.count(h) > 1})
    if dup:
        fail("в новой таблице повторяются колонки: " + ", ".join(f"«{c}»" for c in dup))
    if lack:
        kit.log.warn("в таблице нет колонок: " + ", ".join(f"«{c}»" for c in lack)
                     + (", оставлены пустыми" if p.missing == "empty" else ", пропущены"))

    path = kit.output_dir / "table.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow([text_of(row.get(old)) for old, _ in pairs])
    kit.artifact(path, "Таблица с выбранными колонками", "text/csv")
    renamed = sum(1 for old, new in pairs if old != new)
    kit.result("ok", f"колонок: {len(header)}, переименовано: {renamed}, строк: {len(rows)}", main="table.csv")


main()
