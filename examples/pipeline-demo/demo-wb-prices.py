# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "demo-wb-prices"
# title = "Демо: цены WB"
# description = "Без браузера и сайта: пишет prices.csv с ценами. Источник для демо проекта."
# site = "Демо"
# tags = ["Демо", "Проекты"]
# version = 2
#
# [[params]]
# name = "outcome"
# type = "enum"
# title = "Чем закончить"
# default = "ok"
# choices = [
#   { value = "ok", title = "Готово" },
#   { value = "partial", title = "Частично" },
#   { value = "failed", title = "Упасть" },
#   { value = "failed_once", title = "Упасть один раз" },
# ]
#
# [[params]]
# name = "minutes"
# type = "float"
# title = "Сколько минут работать"
# description = "0: закончить сразу. Больше нуля, чтобы успеть остановить запуск проекта"
# default = 0
# min = 0
# max = 120
#
# [[params]]
# name = "rows"
# type = "int"
# title = "Сколько товаров"
# default = 5
# min = 0
# max = 1000
#
# [[params]]
# name = "broken"
# type = "bool"
# title = "Сломать файл: колонка cost вместо price"
# description = "Для проверки несовпадения колонок перед стартом получателя"
# default = false
#
# [[outputs]]
# name = "prices.csv"
# title = "Цены"
# columns = ["sku", "price"]
# main = true
# ///

"""Демо проекта: «собирает» цены без браузера, пишет prices.csv (sku, price).

«Упасть один раз» помнит падение файлом во временном каталоге системы (dawg-replay-demo-pipeline):
следующий запуск, например «Перезапустить с упавшего», проходит.
"""

import csv
import tempfile
import time
from pathlib import Path

import dawg_replay_kit as kit

BASE, STEP = 100, 10  # у Ozon другие цены, чтобы обновление выбирало меньшую

p = kit.params
kit.stage("Собираю цены")
total = round(p.minutes * 60)
end = time.monotonic() + total
while time.monotonic() < end:
    kit.progress("wait", "Работаю, секунд", total - round(end - time.monotonic()), total)
    time.sleep(min(5.0, max(0.0, end - time.monotonic())))
mark = Path(tempfile.gettempdir()) / "dawg-replay-demo-pipeline" / "demo-wb-prices.failed"
once = p.outcome == "failed_once" and not mark.exists()
if once:
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text("упал один раз", encoding="utf-8")
if p.outcome == "failed" or once:
    kit.log.error("демо: источник упал, как просили")
    kit.result("failed", "упал по просьбе")
    raise SystemExit(1)
path = kit.output_dir / "prices.csv"
with open(path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sku", "cost" if p.broken else "price"])
    for i in range(1, p.rows + 1):
        w.writerow([f"SKU-{i}", BASE + i * STEP])
kit.artifact(path, "Цены", "text/csv")
status = "partial" if p.outcome == "partial" else "ok"
summary = f"цен: {p.rows}" + (", часть не собрана" if status == "partial" else "")
kit.result(status, summary, count=p.rows, main="prices.csv")
