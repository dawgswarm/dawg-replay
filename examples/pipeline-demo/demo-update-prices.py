# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "demo-update-prices"
# title = "Демо: обновить мои цены"
# description = "Без браузера и сайта: берёт цены WB и Ozon, «ставит» меньшую минус 1 и пишет updated.csv. Получатель в демо связей."
# site = "Демо"
# tags = ["Демо", "Связи"]
# version = 1
#
# [[params]]
# name = "wb"
# type = "table"
# title = "Цены WB"
# required = true
# columns = ["sku", "price"]
# column_types = { price = "float" }
#
# [[params]]
# name = "ozon"
# type = "table"
# title = "Цены Ozon"
# columns = ["sku", "price"]
# column_types = { price = "float" }
#
# [[outputs]]
# name = "updated.csv"
# title = "Новые цены"
# columns = ["sku", "old_price", "new_price"]
# main = true
# ///

"""Демо связей (M6): получатель с двумя источниками. Ozon необязателен: без него берётся только WB."""

import csv

import dawg_replay_kit as kit

wb = {r["sku"]: r["price"] for r in kit.params.wb}
ozon = {r["sku"]: r["price"] for r in (kit.params.ozon or [])}
kit.log.info(f"цен WB: {len(wb)}, цен Ozon: {len(ozon)}")
path = kit.output_dir / "updated.csv"
with open(path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sku", "old_price", "new_price"])
    for sku, price in wb.items():
        best = min(price, ozon.get(sku, price))
        new = round(best - 1, 2)
        w.writerow([sku, price, new])
        kit.item(sku, "ok", f"{price:g} → {new:g}")
kit.artifact(path, "Новые цены", "text/csv")
kit.result("ok", f"обновлено цен: {len(wb)}", count=len(wb), main="updated.csv")
