# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "the-internet-actions"
# title = "Учебный сайт: вход и проверка кабинета по таблице"
# description = "Демо действий по таблице на the-internet.herokuapp.com: вход, сохранённый вход, вопрос человеку, отчёт по строкам."
# site = "the-internet"
# tags = ["Демо", "Кабинет"]
# version = 4
# timeout_minutes = 30
#
# [[params]]
# name = "accounts"
# type = "table"
# title = "Аккаунты"
# description = "Колонки login и password. Рабочий аккаунт написан на странице входа учебного сайта"
# required = true
# columns = ["login", "password"]
# max_rows = 20
# secret = true
# input = ["file", "manual", "preset"]
#
# [[params]]
# name = "confirm_first"
# type = "bool"
# title = "Спросить подтверждение перед началом"
# default = true
#
# [[params]]
# name = "ask_code"
# type = "bool"
# title = "Спросить учебный код при первом входе"
# description = "Сайт кода не спрашивает: вопрос показывает, как скрипт ждёт код из SMS. Верный ответ 1234"
# default = true
# ///
"""Демо для приёмки M3: действия по таблице на учебном сайте.

Разведка 15.11.2026, https://the-internet.herokuapp.com:
- вход: форма `#login` на /login (`#username`, `#password`, кнопка submit),
  POST /authenticate, успех ведёт на /secure с `#flash.success` и ссылкой /logout,
  ошибка остаётся на /login с `#flash.error` («Your username is invalid!»);
- вход держится в cookie `rack.session`, kit сохраняет её между запусками;
- проверок сайта и кода подтверждения нет, код спрашивается для демонстрации.

Каждая строка: войти (или взять сохранённый вход), открыть /secure, сверить
заголовок. Строка с неверным паролем падает, остальные идут дальше. Ничего на
сайте не меняется, поэтому проверка «уже сделано» здесь не нужна, см. template_actions.py.
"""

from __future__ import annotations

import time
from typing import Any

import dawg_replay_kit as kit

ORIGIN = "https://the-internet.herokuapp.com"
LOGIN_URL = ORIGIN + "/login"
SECURE_URL = ORIGIN + "/secure"
LOGGED_IN = 'a[href="/logout"]'
LOGIN_ERROR = "#flash.error"
DEMO_CODE = "1234"

PAGE_TIMEOUT_MS = 60_000
STEP_TIMEOUT_MS = 20_000
PAUSE_BETWEEN_ROWS_S = 1


class RowFailed(Exception):
    pass


def flash_text(raw: str) -> str:
    """«Your password is invalid!\n×» в «Your password is invalid!»."""
    return " ".join(raw.replace("×", " ").split())


def logged_in(page: Any) -> bool:
    return page.locator(LOGGED_IN).count() > 0


def log_in(page: Any, row: dict[str, str], code_asked: list[bool]) -> None:
    page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT_MS)
    if kit.params.ask_code and not code_asked:
        code_asked.append(True)
        code = kit.ask(f"Учебный код подтверждения для строки {row['_row']} (верный: {DEMO_CODE})",
                       kind="text", secret=True, timeout_minutes=10)
        if str(code).strip() != DEMO_CODE:
            raise RowFailed("неверный код подтверждения")
    page.fill("#username", row["login"])
    page.fill("#password", row["password"])
    page.click('#login button[type="submit"]')
    page.wait_for_selector(f"{LOGGED_IN}, {LOGIN_ERROR}", timeout=STEP_TIMEOUT_MS)
    if not logged_in(page):
        raise RowFailed(f"вход не выполнен: {flash_text(page.locator(LOGIN_ERROR).inner_text())}")


def process(row: dict[str, str], code_asked: list[bool]) -> str:
    with kit.browser(login=row["login"]) as b:
        page = b.page
        page.goto(SECURE_URL, timeout=PAGE_TIMEOUT_MS)
        restored = logged_in(page)
        if not restored:
            if b.login_restored:
                kit.log.info(f"строка {row['_row']}: сохранённый вход устарел, входим заново")
            try:
                log_in(page, row, code_asked)
            except RowFailed:
                b.forget_login()  # неудачный вход не сохраняем
                error_shot(page, row)
                raise
            b.save_login()  # сразу после входа: не потеряется, даже если запуск убьют дальше
            page.goto(SECURE_URL, timeout=PAGE_TIMEOUT_MS)
        title = page.locator("h2").inner_text(timeout=STEP_TIMEOUT_MS).strip()
        if title != "Secure Area":
            raise RowFailed(f"в кабинете неожиданный заголовок «{title}»")
        return "кабинет открыт" + (" (сохранённый вход)" if restored else ", вход выполнен")


def error_shot(page: Any, row: dict[str, str]) -> None:
    """Скриншот страницы, где строка упала: на экране запуска его видно миниатюрой."""
    path = kit.output_dir / f"row-{row['_row']}-error.png"
    try:
        page.screenshot(path=str(path))
    except Exception:
        return
    kit.artifact(path, f"Скриншот: строка {row['_row']} не получилась", "image/png")


def rows_dative(n: int) -> str:
    """«по 1 строке», «по 5 строкам»."""
    return f"{n} строке" if n % 10 == 1 and n % 100 != 11 else f"{n} строкам"


def main() -> None:
    p = kit.params
    rows = p.accounts
    total = len(rows)
    if p.confirm_first and not kit.ask(f"Пройти по {rows_dative(total)} таблицы на учебном сайте?", kind="confirm"):
        kit.result("failed", "проход не подтверждён")
        raise SystemExit(1)

    kit.set_browser_limit(1)
    kit.stage("Идём по строкам")
    counts = {"ok": 0, "failed": 0}
    code_asked: list[bool] = []
    # Отчёт по строкам (экран запуска и «Скачать отчёт CSV») приложение строит из kit.item.
    for i, row in enumerate(rows, start=1):
        try:
            status, msg = "ok", process(row, code_asked)
        except kit.AskTimeout:
            status, msg = "failed", "не дождались кода"
        except RowFailed as e:
            status, msg = "failed", str(e)
        except Exception as e:  # отмена (SystemExit) сюда не попадает
            status, msg = "failed", f"{type(e).__name__}: {e}"
        counts[status] += 1
        kit.item(row["_row"], status, msg, row=row["_row"])  # таблица секретная: ключ это номер строки
        kit.progress("rows", "Строки таблицы", i, total)
        time.sleep(PAUSE_BETWEEN_ROWS_S)

    text = f"кабинет открыт: {counts['ok']} из {total}" + (f", не вышло: {counts['failed']}" if counts["failed"] else "")
    if not counts["failed"]:
        kit.result("ok", text, count=counts["ok"])
    elif counts["ok"]:
        kit.result("partial", text, count=counts["ok"])
    else:
        kit.result("failed", text)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
