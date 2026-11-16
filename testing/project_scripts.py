"""Скрипты-заглушки для тестов проектов (test_projects): источник с объявленными выходами и получатель."""

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
# name = "shop"
# type = "string"
# title = "Магазин"
#
# [[params]]
# name = "pages"
# type = "int"
# title = "Страницы"
# default = 1
#
# [[outputs]]
# name = "cards.csv"
# title = "Карточки"
# columns = [{columns}]
# main = true
#
# [[outputs]]
# name = "report.pdf"
# optional = true
# ///

import dawg_replay_kit as kit

(kit.output_dir / "cards.csv").write_text("sku,name\\nA1,x\\n", encoding="utf-8")
kit.result("ok", "готово", main="cards.csv")
'''

TARGET = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "{name}"
# title = "Выгрузка в Google"
# version = 1
#
# [[params]]
# name = "cards"
# type = "table"
# title = "Карточки"
# required = true
# columns = ["sku", "name"]
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
# required = true
#
# [[params]]
# name = "token"
# type = "secret"
# title = "Токен"
#
# [[params]]
# name = "sheet"
# type = "string"
# title = "Лист"
# required = true
# ///

import dawg_replay_kit as kit

kit.result("ok", "ок")
'''
