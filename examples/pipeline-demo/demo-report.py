# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "demo-report"
# title = "Демо: отчёт по ценам"
# description = "Без браузера и сайта: сводка по новым ценам в report.csv. Конец цепочки в демо связей."
# site = "Демо"
# tags = ["Демо", "Связи"]
# version = 1
#
# [[params]]
# name = "updated"
# type = "table"
# title = "Новые цены"
# required = true
# columns = ["sku", "new_price"]
# column_types = { new_price = "float" }
#
# [[outputs]]
# name = "report.csv"
# title = "Отчёт"
# columns = ["товаров", "средняя_цена"]
# main = true
# ///

"""Демо связей (M6): последний шаг цепочки WB + Ozon → обновление → отчёт."""

import csv

import dawg_replay_kit as kit

rows = kit.params.updated
avg = round(sum(r["new_price"] for r in rows) / len(rows), 2) if rows else 0
path = kit.output_dir / "report.csv"
with open(path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["товаров", "средняя_цена"])
    w.writerow([len(rows), avg])
kit.rows("report", [{"товаров": len(rows), "средняя_цена": avg}])
kit.artifact(path, "Отчёт", "text/csv")
kit.result("ok", f"товаров {len(rows)}, средняя цена {avg:g}", count=len(rows), main="report.csv")
