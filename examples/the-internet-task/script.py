# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "the-internet-task"
# title = "Учебный сайт: проверка страницы и одно действие"
# description = "Демо задачи без таблицы на the-internet.herokuapp.com: скриншот страницы, по выбору нажать Start и дождаться результата."
# site = "the-internet"
# tags = ["Демо", "Проверка"]
# version = 1
# timeout_minutes = 10
#
# [[params]]
# name = "mode"
# type = "enum"
# title = "Что сделать"
# default = "check"
# choices = [
#   { value = "check", title = "Только посмотреть и прислать скриншот" },
#   { value = "act", title = "Нажать Start и дождаться результата" },
# ]
#
# [[params]]
# name = "confirm"
# type = "bool"
# title = "Спросить подтверждение перед нажатием"
# default = true
# ///
"""Демо для M4: задача без таблицы, скриншот как доказательство, итог с count, main и links.

Разведка 15.11.2026, https://the-internet.herokuapp.com/dynamic_loading/2:
- кнопка `#start button`; после нажатия «Loading…», затем появляется `#finish h4`
  с текстом «Hello World!» (около 5 секунд);
- страница не помнит состояние между загрузками, поэтому «уже сделано» здесь
  означает только «результат уже на странице»;
- проверок сайта нет.
"""

from __future__ import annotations

import dawg_replay_kit as kit

URL = "https://the-internet.herokuapp.com/dynamic_loading/2"
START = "#start button"
FINISH = "#finish h4"
PAGE_TIMEOUT_MS = 60_000
FINISH_TIMEOUT_MS = 30_000


def shot(page, name: str, label: str) -> str:
    path = kit.output_dir / name
    page.screenshot(path=str(path))
    kit.artifact(path, label, "image/png")
    return name


def main() -> None:
    p = kit.params
    kit.set_browser_limit(1)
    action = None
    with kit.browser() as b:
        page = b.page
        kit.stage("Открываем страницу")
        page.goto(URL, timeout=PAGE_TIMEOUT_MS)
        heading = page.locator("h4").first.inner_text().strip()
        main_file = shot(page, "before.png", "Скриншот: страница при открытии")

        if p.mode == "act":
            kit.stage("Нажимаем Start")
            if page.locator(FINISH).count() and page.locator(FINISH).first.is_visible():
                action = "результат уже был на странице"
            elif p.confirm and not kit.ask("Нажать Start на учебной странице?", kind="confirm"):
                action = "не подтверждено"
            else:
                page.click(START)
                page.wait_for_selector(FINISH, state="visible", timeout=FINISH_TIMEOUT_MS)
                action = f"нажато, на странице «{page.locator(FINISH).first.inner_text().strip()}»"
                main_file = shot(page, "after.png", "Скриншот: результат после нажатия")

    summary = f"заголовок «{heading}»" + (f", {action}" if action else "")
    kit.result("ok", summary, count=1, main=main_file, links=[{"title": "Учебная страница", "url": URL}])


if __name__ == "__main__":
    main()
