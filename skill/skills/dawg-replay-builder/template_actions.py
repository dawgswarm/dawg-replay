# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.2,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///

# /// dawg-script
# contract = "1.2"
# name = "example-cabinet-actions"
# title = "Пример: действие в кабинете по списку аккаунтов"
# description = "Для каждой строки таблицы входит в кабинет, проверяет, сделано ли действие, и делает его."
# site = "Пример"
# tags = ["Кабинет", "Действия"]
# version = 1
# timeout_minutes = 60
#
# [[params]]
# name = "accounts"
# type = "table"
# title = "Аккаунты и что сделать"
# description = "Excel или CSV: логин, пароль и значение для каждого аккаунта"
# required = true
# columns = ["login", "password", "value"]
# max_rows = 500
# # Пароли в таблице: вся таблица секретная, в пресеты и историю не попадает
# secret = true
#
# [[params]]
# name = "confirm_first"
# type = "bool"
# title = "Спросить подтверждение перед первым изменением"
# default = true
#
# [[params]]
# name = "check_only"
# type = "bool"
# title = "Только проверить, ничего не менять"
# default = false
#
# [[params]]
# name = "geo"
# type = "string"
# title = "Город браузера"
# description = "Slug города DAWG. Если пусто, браузер без привязки к городу"
# default = ""
# ///
"""Действие в кабинете сайта по строкам таблицы. Эталон для dawg-replay-builder.

Разведка: ЗАМЕНИТЬ на дату и находки (адрес входа, как сайт показывает, что вход
выполнен, спрашивает ли код, где проверить, что действие уже сделано, что
меняет действие, как сайт подтверждает успех).

Приёмы:
- одна строка таблицы обрабатывается целиком: вход, проверка, действие, сверка;
  упавшая строка не останавливает остальные, итог по каждой через `kit.item`;
- вход сохраняется между запусками (`kit.browser(login=...)`): повторный запуск
  не входит заново, если сайт не отозвал сессию;
- перед действием скрипт проверяет, не сделано ли оно уже, поэтому повторный
  запуск и «Перезапустить упавшие» не создают дублей;
- первое изменение на сайте подтверждает человек (`kit.ask(..., kind="confirm")`),
  код из SMS или письма тоже спрашивается у человека (`kit.ask(..., secret=True)`);
- пароли только в таблице с `secret = true`: kit маскирует её значения в событиях,
  поэтому ключ строки в отчёте это номер строки файла, а не логин.

Проверки сайта браузер DAWG проходит сам, скрипт только ждёт.
"""

from __future__ import annotations

import csv
import time
from typing import Any

import dawg_replay_kit as kit

# ЗАМЕНИТЬ: адреса и признаки на страницах.
ORIGIN = "https://cabinet.example.ru"
LOGIN_URL = ORIGIN + "/login"
TARGET_URL = ORIGIN + "/settings"  # где делается действие
LOGGED_IN = '[data-test="profile-menu"]'  # есть только у вошедшего
LOGIN_FIELD = 'input[name="login"]'
PASSWORD_FIELD = 'input[name="password"]'
SUBMIT = 'button[type="submit"]'
CODE_FIELD = 'input[autocomplete="one-time-code"]'  # поле кода, если сайт его спрашивает
LOGIN_ERROR = '[role="alert"]'  # сообщение сайта о неверном логине или пароле
VALUE_FIELD = 'input[name="value"]'
SAVE = 'button[data-test="save"]'
SAVED = '[data-test="saved"]'  # подтверждение сайта, что сохранено

PAGE_TIMEOUT_MS = 60_000
STEP_TIMEOUT_MS = 20_000
CODE_TIMEOUT_MIN = 10  # сколько ждать код от человека
PAUSE_BETWEEN_ROWS_S = 2  # умеренный темп, сайт видит обычного пользователя

REPORT_COLUMNS = ["row", "status", "message"]
ALREADY = "уже сделано"


class RowFailed(Exception):
    """Строку сделать нельзя: текст для отчёта, без паролей и кодов."""


# --- шаги на сайте: ЗАМЕНИТЬ под сайт --------------------------------------------


def logged_in(page: Any) -> bool:
    return page.locator(LOGGED_IN).count() > 0


def log_in(page: Any, row: dict[str, str]) -> None:
    """Вход логином и паролем; код спрашивается у человека, если сайт его просит."""
    page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT_MS)
    page.fill(LOGIN_FIELD, row["login"])
    page.fill(PASSWORD_FIELD, row["password"])
    page.click(SUBMIT)
    page.wait_for_selector(f"{LOGGED_IN}, {CODE_FIELD}, {LOGIN_ERROR}", timeout=STEP_TIMEOUT_MS)
    if page.locator(CODE_FIELD).count():
        code = kit.ask(
            f"Код подтверждения для аккаунта из строки {row['_row']}",
            kind="text", secret=True, timeout_minutes=CODE_TIMEOUT_MIN,
        )
        page.fill(CODE_FIELD, str(code).strip())
        page.click(SUBMIT)
        page.wait_for_selector(f"{LOGGED_IN}, {LOGIN_ERROR}", timeout=STEP_TIMEOUT_MS)
    if not logged_in(page):
        error = page.locator(LOGIN_ERROR).first.inner_text(timeout=2000) if page.locator(LOGIN_ERROR).count() else ""
        raise RowFailed(f"вход не выполнен: {error.strip() or 'сайт не пустил'}")


def already_done(page: Any, row: dict[str, str]) -> bool:
    """Действие уже сделано (прошлым запуском или руками): повторять нельзя."""
    page.goto(TARGET_URL, timeout=PAGE_TIMEOUT_MS)
    return page.input_value(VALUE_FIELD, timeout=STEP_TIMEOUT_MS) == row["value"]


def do_action(page: Any, row: dict[str, str]) -> None:
    page.fill(VALUE_FIELD, row["value"])
    page.click(SAVE)
    page.wait_for_selector(SAVED, timeout=STEP_TIMEOUT_MS)


def verify(page: Any, row: dict[str, str]) -> None:
    """Сверка по сайту после действия: сайт мог показать «сохранено», но не сохранить."""
    page.reload(timeout=PAGE_TIMEOUT_MS)
    if page.input_value(VALUE_FIELD, timeout=STEP_TIMEOUT_MS) != row["value"]:
        raise RowFailed("сайт показал «сохранено», но после обновления значение прежнее")


# --- одна строка -------------------------------------------------------------------


def process(row: dict[str, str], p: Any, allowed: Any) -> tuple[str, str]:
    """(статус, сообщение) для отчёта. `allowed()` спрашивает подтверждение один раз."""
    with kit.browser(login=row["login"], geo=p.geo or None) as b:
        page = b.page
        page.goto(TARGET_URL, timeout=PAGE_TIMEOUT_MS)
        if not logged_in(page):
            if b.login_restored:
                kit.log.info(f"строка {row['_row']}: сохранённый вход устарел, входим заново")
            try:
                log_in(page, row)
            except RowFailed:
                b.forget_login()  # неудачный вход не сохраняем, в списке входов его не будет
                raise
            b.save_login()  # сразу после входа: вход не потеряется, даже если запуск убьют дальше
        if already_done(page, row):
            return "skipped", ALREADY
        if p.check_only:
            return "skipped", "нужно сделать (режим проверки, ничего не менялось)"
        if not allowed():
            return "skipped", "не подтверждено"
        do_action(page, row)
        verify(page, row)
        return "ok", "сделано"


def summary(counts: dict[str, int], total: int) -> str:
    """«из 50: сделано 46, уже было 2, не вышло 2». Ключи: ok, already, failed, skipped."""
    parts = [f"сделано {counts.get('ok', 0)}"]
    if counts.get("already"):
        parts.append(f"уже было {counts['already']}")
    if counts.get("failed"):
        parts.append(f"не вышло {counts['failed']}")
    if counts.get("skipped"):
        parts.append(f"пропущено {counts['skipped']}")
    return f"из {total}: " + ", ".join(parts)


# --- запуск -------------------------------------------------------------------------


def main() -> None:
    p = kit.params
    rows = p.accounts
    total = len(rows)
    counts: dict[str, int] = {}
    decision: list[bool] = []  # ответ на подтверждение, спрашиваем один раз

    def allowed() -> bool:
        if not p.confirm_first:
            return True
        if not decision:
            decision.append(bool(kit.ask(f"Начать изменения на сайте? Строк в таблице: {total}", kind="confirm")))
        return decision[0]

    report_path = kit.output_dir / "report.csv"
    kit.set_browser_limit(1)  # строки по очереди: у каждой свой вход
    kit.stage("Идём по строкам")
    with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        w.writeheader()
        for i, row in enumerate(rows, start=1):
            try:
                status, msg = process(row, p, allowed)
            except kit.AskTimeout:
                status, msg = "failed", f"не дождались кода за {CODE_TIMEOUT_MIN} мин"
            except RowFailed as e:
                status, msg = "failed", str(e)
            except Exception as e:  # отмена (SystemExit) сюда не попадает
                status, msg = "failed", f"{type(e).__name__}: {e}"
            kind = "already" if msg == ALREADY else status
            counts[kind] = counts.get(kind, 0) + 1
            kit.item(row["_row"], status, msg, row=row["_row"])
            w.writerow({"row": row["_row"], "status": status, "message": msg})
            f.flush()
            kit.progress("rows", "Строки таблицы", i, total)
            if decision == [False]:
                kit.log.warn("изменения не подтверждены, остальные строки не трогаем")
                break
            time.sleep(PAUSE_BETWEEN_ROWS_S)
    kit.artifact(report_path, "Отчёт по строкам (CSV)", "text/csv")

    text = summary(counts, total)
    if decision == [False]:
        kit.result("failed", f"изменения не подтверждены. {text}")
        raise SystemExit(1)
    if not counts.get("failed"):
        kit.result("ok", text)
    elif counts["failed"] < total:
        kit.result("partial", text)
    else:
        kit.result("failed", text)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
