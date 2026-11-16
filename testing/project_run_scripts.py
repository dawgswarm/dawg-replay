"""Скрипты-заглушки для тестов запуска проектов (test_project_runs): источник и получатель без браузера."""

SOURCE = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "{name}"
# title = "{title}"
# version = 1
#
# [[params]]
# name = "outcome"
# type = "enum"
# title = "Исход"
# choices = ["ok", "partial", "failed"]
# default = "ok"
#
# [[params]]
# name = "csv"
# type = "string"
# title = "Таблица"
# default = "sku,name\\\\nA1,x"
#
# [[params]]
# name = "emit"
# type = "string"
# title = "Ещё файл"
# default = ""
#
# [[outputs]]
# name = "cards.csv"
# title = "Карточки"
# columns = ["sku", "name"]
# main = true
# ///

import dawg_replay_kit as kit

p = kit.params
(kit.output_dir / "cards.csv").write_text(p["csv"].replace("\\\\n", "\\n") + "\\n", encoding="utf-8")
if p["emit"]:
    (kit.output_dir / p["emit"]).write_text("sku\\nA1\\n", encoding="utf-8")
if p["outcome"] == "failed":
    raise SystemExit("источник упал по просьбе теста")
kit.result(p["outcome"], "готово", main="cards.csv")
'''

TARGET = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "{name}"
# title = "{title}"
# version = 1
#
# [[params]]
# name = "cards"
# type = "table"
# title = "Карточки"
# required = true
# columns = ["sku"]
#
# [[params]]
# name = "sheet"
# type = "string"
# title = "Лист"
# default = "по умолчанию"
#
# [[outputs]]
# name = "got.json"
# title = "Что получил"
# main = true
# ///

import json

import dawg_replay_kit as kit

rows = [{{k: v for k, v in r.items() if not k.startswith("_")}} for r in kit.params["cards"]]
(kit.output_dir / "got.json").write_text(json.dumps({{"rows": rows, "sheet": kit.params["sheet"]}}, ensure_ascii=False),
                                         encoding="utf-8")
kit.result("ok", f"строк {{len(rows)}}", main="got.json")
'''
