# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "demo-city-prices"
# title = "Демо: цены в городе"
# description = "Без браузера и сайта: «собирает» цены в одном городе. На городе из «Упасть на» падает один раз, повтор проходит."
# site = "Демо"
# tags = ["Демо", "Проекты"]
# version = 1
#
# [[params]]
# name = "city"
# type = "string"
# title = "Город"
# required = true
#
# [[params]]
# name = "token"
# type = "secret"
# title = "Токен кабинета"
# required = true
#
# [[params]]
# name = "shop"
# type = "string"
# title = "Магазин"
# default = "без названия"
#
# [[params]]
# name = "fail_once"
# type = "string"
# title = "Упасть на"
# description = "Город, на котором первый запуск упадёт; следующий пройдёт"
# default = "Казань"
#
# [[outputs]]
# name = "prices.csv"
# title = "Цены"
# columns = ["sku", "price", "shop"]
# main = true
# ///

"""Падение «один раз» помнится файлом во временном каталоге системы: после перезапуска строка проходит."""

import csv
import tempfile
import zlib
from pathlib import Path

import dawg_replay_kit as kit

p = kit.params
city = p["city"]
if not p["token"].startswith("demo-"):
    kit.result("failed", "токен не от «Демо: вход в кабинет»")
    raise SystemExit(0)
mark = Path(tempfile.gettempdir()) / "dawg-replay-demo-rows" / f"{zlib.crc32(city.encode())}.failed"
if city == p["fail_once"] and not mark.exists():
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(city, encoding="utf-8")
    raise SystemExit(f"{city}: страница не открылась (демо-падение, повтор пройдёт)")
base = zlib.crc32(city.encode()) % 900 + 100
with open(kit.output_dir / "prices.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sku", "price", "shop"])
    for i in range(3):
        w.writerow([f"SKU-{i + 1}", base + i * 10, p["shop"]])
kit.result("ok", f"{city}: 3 цены", main="prices.csv", count=3)
