"""Скрипты-заглушки для тестов передачи файлов результата (test_transfer, test_web_transfer): источник с файлами и получатель."""

SOURCE = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "{name}"
# title = "{title}"
# version = 1
#
# [[params]]
# name = "csv"
# type = "text"
# title = "Содержимое"
# default = "sku,price\\nA1,10\\nB2,20"
#
# [[params]]
# name = "outcome"
# type = "enum"
# title = "Итог"
# choices = ["ok", "partial", "failed"]
# default = "ok"
# ///

import time
import dawg_replay_kit as kit

out = kit.output_dir
(out / "prices.csv").write_text(kit.params.csv + "\\n", encoding="utf-8")
(out / "report-1.txt").write_text("первый", encoding="utf-8")
time.sleep(0.02)
(out / "report-2.txt").write_text("второй", encoding="utf-8")
kit.result(kit.params.outcome, "готово", main="prices.csv")
'''

TARGET = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "{name}"
# title = "Обновить цены"
# version = {version}
#
# [[params]]
# name = "items"
# type = "table"
# title = "Товары"
# required = true
# columns = ["sku"]
# input = ["manual"]
#
# [[params]]
# name = "doc"
# type = "file"
# title = "Отчёт"
#
# [[params]]
# name = "docs"
# type = "file"
# title = "Все отчёты"
# multiple = true
#
# [[params]]
# name = "keys"
# type = "table"
# title = "Ключи"
# secret = true
#
# [[params]]
# name = "note"
# type = "string"
# title = "Пометка"
{extra}# ///

import json
import dawg_replay_kit as kit

(kit.output_dir / "got.json").write_text(json.dumps([r["sku"] for r in kit.params.items]), encoding="utf-8")
kit.result("ok", "ок")
'''
