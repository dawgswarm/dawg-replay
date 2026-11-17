# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "demo-cities"
# title = "Демо: список городов"
# description = "Без браузера и сайта: пишет cities.csv, таблицу городов для прохода по строкам."
# site = "Демо"
# tags = ["Демо", "Проекты"]
# version = 1
#
# [[params]]
# name = "cities"
# type = "string_list"
# title = "Города"
# default = ["Москва", "Казань", "Тверь", "Омск", "Сочи"]
#
# [[params]]
# name = "inactive"
# type = "string_list"
# title = "Неактивные"
# description = "Эти города попадут в таблицу с «Активен» = нет"
# default = ["Омск"]
#
# [[outputs]]
# name = "cities.csv"
# title = "Города"
# columns = ["Город", "Активен"]
# main = true
# ///

import csv

import dawg_replay_kit as kit

p = kit.params
with open(kit.output_dir / "cities.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Город", "Активен"])
    for city in p["cities"]:
        w.writerow([city, "нет" if city in p["inactive"] else "да"])
kit.result("ok", f"городов: {len(p['cities'])}", main="cities.csv", count=len(p["cities"]))
