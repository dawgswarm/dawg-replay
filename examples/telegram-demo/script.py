# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "telegram-demo"
# title = "Демо уведомлений в Telegram"
# description = "Без браузера и сайта: работает заданное время, может задать вопрос и закончиться любым итогом. Для проверки сообщений в Telegram."
# site = "Демо"
# tags = ["Демо", "Telegram"]
# version = 1
# timeout_minutes = 120
#
# [[params]]
# name = "outcome"
# type = "enum"
# title = "Чем закончить"
# default = "ok"
# choices = [
#   { value = "ok", title = "Готово" },
#   { value = "partial", title = "Частично: 2 строки из 5 упали" },
#   { value = "failed", title = "Упасть с объяснением" },
#   { value = "crash", title = "Упасть без объяснения (ошибка в коде)" },
# ]
#
# [[params]]
# name = "minutes"
# type = "float"
# title = "Сколько минут работать"
# description = "0: закончить сразу. Для проверки порога долгого ручного запуска поставьте больше порога"
# default = 0
# min = 0
# max = 100
#
# [[params]]
# name = "ask"
# type = "enum"
# title = "Вопрос посреди запуска"
# default = "none"
# choices = [
#   { value = "none", title = "Без вопроса" },
#   { value = "confirm", title = "Подтверждение: да или нет" },
#   { value = "code", title = "Секретный код (как из SMS)" },
#   { value = "choice", title = "Выбор из вариантов" },
#   { value = "text", title = "Ответ текстом" },
# ]
#
# [[params]]
# name = "ask_minutes"
# type = "int"
# title = "Сколько ждать ответа, минут"
# default = 10
# min = 1
# max = 60
#
# [[params]]
# name = "count"
# type = "int"
# title = "Сколько записей «собрать»"
# description = "0 при итоге «Готово»: приложение заподозрит тихую поломку («ничего не собрал»)"
# default = 50
# min = 0
# max = 100000
# ///
"""Демо для приёмки уведомлений в Telegram (docs/acceptance/m5-telegram.md).

Браузер не открывается, минуты DAWG не тратятся. Скрипт ждёт заданное время, по желанию
задаёт вопрос (`kit.ask`), пишет файл `result.csv` и заканчивается выбранным итогом.
Секретный ответ скрипт не печатает и не сохраняет, в журнал попадает только его длина.
"""

from __future__ import annotations

import csv
import time

import dawg_replay_kit as kit

LINK = {"title": "Как устроены уведомления", "url": "https://core.telegram.org/bots"}


def wait(minutes: float) -> None:
    """Работает `minutes` минут, раз в несколько секунд двигает прогресс."""
    total = max(0.0, minutes) * 60
    if not total:
        return
    kit.stage("Работаем")
    start = time.monotonic()
    while (done := time.monotonic() - start) < total:
        kit.progress("time", "Прошло времени", int(done), int(total))
        time.sleep(min(5.0, total - done))
    kit.progress("time", "Прошло времени", int(total), int(total))


def ask(kind: str, minutes: int) -> str | None:
    """Задаёт вопрос; возвращает строку для итога или None без вопроса."""
    if kind == "none":
        return None
    kit.stage("Ждём ответа")
    if kind == "confirm":
        yes = kit.ask("Демо: продолжить запуск?", kind="confirm", timeout_minutes=minutes)
        return "подтверждено" if yes else "отказались"
    if kind == "code":
        code = kit.ask("Демо: введите любой код, например 1234", kind="text", secret=True, timeout_minutes=minutes)
        kit.log.info(f"код получен, символов: {len(str(code))}")
        return f"код получен ({len(str(code))} симв.)"
    if kind == "choice":
        city = kit.ask("Демо: какой город?", kind="choice", choices=["Москва", "Казань", "Петербург"],
                       timeout_minutes=minutes)
        return f"выбран город {city}"
    text = kit.ask("Демо: напишите пару слов", kind="text", timeout_minutes=minutes)
    return f"ответ «{text}»"


def main() -> None:
    p = kit.params
    wait(p.minutes)
    try:
        answer = ask(p.ask, p.ask_minutes)
    except kit.AskTimeout:
        kit.result("failed", f"не дождались ответа за {p.ask_minutes} мин.")
        raise SystemExit(1) from None

    path = kit.output_dir / "result.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "value"])
        w.writerows([i, i * 10] for i in range(1, min(p.count, 1000) + 1))
    kit.artifact(path, "Результат (CSV)", "text/csv")

    tail = f", {answer}" if answer else ""
    if p.outcome == "crash":
        raise RuntimeError("демо: ошибка в коде скрипта")
    if p.outcome == "failed":
        kit.result("failed", f"сайт демо не ответил{tail}")
        raise SystemExit(1)
    if p.outcome == "partial":
        for n in range(1, 6):
            ok = n not in (2, 4)
            kit.item(n, "ok" if ok else "failed", None if ok else "демо: строка не прошла")
        kit.result("partial", f"3 из 5 строк, 2 упали{tail}", count=3, main="result.csv", links=[LINK])
        return
    kit.result("ok", f"собрано {p.count}{tail}", count=p.count, main="result.csv", links=[LINK])


if __name__ == "__main__":
    main()
