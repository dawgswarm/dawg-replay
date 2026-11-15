# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "example-page-task"
# title = "Пример: проверка страницы и одно действие"
# description = "Открывает страницу, читает значение, при условии делает одно действие и сохраняет скриншот как доказательство."
# site = "Пример"
# tags = ["Проверка", "Действие"]
# version = 1
# timeout_minutes = 15
#
# [[params]]
# name = "url"
# type = "url"
# title = "Страница"
# required = true
#
# [[params]]
# name = "mode"
# type = "enum"
# title = "Что сделать"
# default = "check"
# choices = [
#   { value = "check", title = "Только проверить и прислать скриншот" },
#   { value = "act", title = "Проверить и, если условие выполнено, нажать кнопку" },
# ]
#
# [[params]]
# name = "below"
# type = "float"
# title = "Условие: значение ниже"
# description = "Пусто: условия нет, действие делается всегда (в режиме «нажать кнопку»)"
#
# [[params]]
# name = "day"
# type = "date"
# title = "За какой день"
# description = "Для регулярного запуска оставьте «вчера»: дата считается в момент запуска"
# default = "today-1"
#
# [[params]]
# name = "attachment"
# type = "file"
# title = "Файл для формы"
# description = "Если действие отправляет документ: PDF или картинка"
# accept = [".pdf", "image/*"]
# max_mb = 10
# ///
"""Проверка страницы и одно действие. Эталон для dawg-replay-builder (задачи без таблицы).

Разведка: ЗАМЕНИТЬ на дату и находки (адрес страницы, где лежит значение, как сайт
показывает, что действие уже сделано, признак успеха, какие проверки показывает сайт).

Подходит для «посмотри и пришли скриншот», «если цена упала, нажми», «отправь одну
заявку». По строкам таблицы: template_actions.py, сбор многих карточек: template.py.

Приёмы:
- скриншот как доказательство: `kit.artifact(..., "image/png")`, его видно на экране запуска;
- итог с `count` (сколько значений прочитано), `main` (главный файл) и `links` (страница);
- первое изменение на сайте подтверждает человек (`kit.ask(..., kind="confirm")`);
- перед действием проверка «уже сделано»: повторный запуск не дублирует его;
- дата «за вчера» относительная (`default = "today-1"`), регулярный запуск берёт свою.

Проверки сайта браузер DAWG проходит сам, скрипт только ждёт.
"""

from __future__ import annotations

import json
import re
from typing import Any

import dawg_replay_kit as kit

# ЗАМЕНИТЬ: признаки на странице.
VALUE = '[data-test="price"]'  # где лежит значение, которое проверяем
ACTION = 'button[data-test="confirm"]'  # кнопка действия
DONE = '[data-test="confirmed"]'  # признак, что действие уже сделано
FILE_INPUT = 'input[type="file"]'  # поле загрузки, если действие отправляет документ

PAGE_TIMEOUT_MS = 60_000
STEP_TIMEOUT_MS = 20_000


def number(text: str) -> float | None:
    """Число из текста страницы: «1 299,50 ₽» -> 1299.5. Нет числа: None."""
    m = re.search(r"-?\d[\d\s  ]*(?:[.,]\d+)?", text or "")
    if not m:
        return None
    return float(re.sub(r"[\s  ]", "", m.group(0)).replace(",", "."))


def should_act(p: Any, value: float | None) -> bool:
    """Делать ли действие: режим «нажать» и условие выполнено (или условия нет)."""
    if p.mode != "act":
        return False
    if p.below is None:
        return True
    return value is not None and value < p.below


def shot(page: Any, name: str, label: str) -> str:
    path = kit.output_dir / name
    page.screenshot(path=str(path), full_page=True)
    kit.artifact(path, label, "image/png")
    return name


def main() -> None:
    p = kit.params
    kit.set_browser_limit(1)
    observation: dict[str, Any] = {"url": p.url, "day": p.day}
    main_file = None
    acted = False
    with kit.browser() as b:
        page = b.page
        kit.stage("Открываем страницу")
        page.goto(p.url, timeout=PAGE_TIMEOUT_MS)
        page.wait_for_selector(VALUE, timeout=STEP_TIMEOUT_MS)
        text = page.locator(VALUE).first.inner_text().strip()
        value = number(text)
        observation.update({"text": text, "value": value})
        kit.metric("value", "Значение на странице", value if value is not None else text)
        main_file = shot(page, "before.png", "Скриншот: страница при проверке")

        if should_act(p, value):
            kit.stage("Действие")
            if page.locator(DONE).count():
                observation["action"] = "уже сделано"
            elif kit.ask(f"Нажать кнопку на {p.url}? Значение сейчас: {text}", kind="confirm"):
                if p.attachment:
                    page.set_input_files(FILE_INPUT, p.attachment["path"])
                page.click(ACTION)
                page.wait_for_selector(DONE, timeout=STEP_TIMEOUT_MS)
                acted = True
                observation["action"] = "сделано"
                main_file = shot(page, "after.png", "Скриншот: действие выполнено")
            else:
                observation["action"] = "не подтверждено"

    (kit.output_dir / "observation.json").write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8")
    kit.artifact(kit.output_dir / "observation.json", "Что прочитано (JSON)", "application/json")
    what = f"значение «{text}»" + (f", действие: {observation['action']}" if "action" in observation else "")
    kit.result("ok", what, count=1 if value is not None or text else 0, main=main_file,
               links=[{"title": "Страница", "url": p.url}])
    if acted:
        kit.log.info("действие выполнено, доказательство: after.png")


if __name__ == "__main__":
    main()
