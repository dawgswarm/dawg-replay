# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.5,<2"]
# ///

# /// dawg-script
# contract = "1.5"
# name = "demo-login"
# title = "Демо: вход в кабинет"
# description = "Без браузера и сайта: «входит» в кабинет и отдаёт токен и название магазина как выходные значения."
# site = "Демо"
# tags = ["Демо", "Проекты"]
# version = 1
#
# [[params]]
# name = "shop"
# type = "string"
# title = "Магазин"
# default = "Демо-магазин"
#
# [[outputs]]
# kind = "value"
# name = "token"
# title = "Токен"
# type = "string"
# secret = true
#
# [[outputs]]
# kind = "value"
# name = "shop"
# title = "Магазин"
# type = "string"
# ///

"""Демо выходных значений: токен секретный, в журнал и на экран запуска не попадает."""

import secrets

import dawg_replay_kit as kit

kit.output("token", "demo-" + secrets.token_hex(4))
kit.output("shop", kit.params["shop"])
kit.result("ok", f"вошли в кабинет «{kit.params['shop']}»")
