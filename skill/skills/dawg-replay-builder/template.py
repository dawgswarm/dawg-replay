# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.1,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///

# /// dawg-script
# contract = "1.1"
# name = "example-search-cards"
# title = "Пример: карточки по поисковому запросу"
# description = "Обходит выдачу сайта по запросу и собирает название, цену, рейтинг и ссылку."
# # site: как сайт называют люди ("Ozon", "Wildberries", "Яндекс Маркет"); tags: 1-3 метки о том, что собирается
# site = "Пример"
# tags = ["Цены", "Карточки"]
# version = 1
# timeout_minutes = 30
#
# [[params]]
# name = "query"
# type = "string"
# title = "Поисковый запрос"
# required = true
#
# [[params]]
# name = "max_pages"
# type = "int"
# title = "Сколько страниц выдачи обойти"
# default = 3
# min = 1
# max = 50
#
# [[params]]
# name = "sort"
# type = "enum"
# title = "Сортировка"
# description = "popular: популярные, price: дешевле, rating: с высоким рейтингом"
# choices = ["popular", "price", "rating"]
# default = "popular"
#
# [[params]]
# name = "only_with_price"
# type = "bool"
# title = "Пропускать карточки без цены"
# default = true
#
# [[params]]
# name = "geo"
# type = "string"
# title = "Город браузера"
# description = "Slug города DAWG: от него зависят цены и наличие. Если пусто, браузер без привязки к городу"
# default = "moskva"
#
# # Секрет для выгрузки (Google-таблица, бот), если она нужна клиенту:
# # [[params]]
# # name = "sheet_token"
# # type = "secret"
# # title = "Токен Google Sheets"
# ///
"""Карточки сайта по поисковому запросу. Эталон для dawg-replay-builder.

Разведка: ЗАМЕНИТЬ на дату и находки (адрес выдачи, эндпоинт, где лежат данные,
как устроена пагинация, что делает антибот, нужен ли geo).

Пример записи по опыту Ozon (14.11.2026):
- поиск проходит JS-проверку «Antibot Challenge Page» сам за несколько секунд и
  перенаправляет в категорию с тем же `text`;
- первая страница лежит в JSON атрибута `data-state` элемента выдачи, следующие
  страница запрашивает при прокрутке у внутреннего API, скрипт перехватывает ответы;
- страница товара сразу показывает слайдер-капчу, поэтому скрипт на неё не ходит.

Приёмы: браузер только через kit (лимит указан явно), данные из ответов API и
состояния страницы, ожидание проверки антибота по признакам на странице, повторы
с паузой и явные таймауты, CSV дописывается после каждой страницы, при падении на
середине итог `partial`. Капчу скрипт не обходит: при ней он останавливается с понятной ошибкой.
"""

from __future__ import annotations

import csv
import json
import time
from typing import Any, Callable, TypeVar
from urllib.parse import parse_qs, quote, urlparse

import dawg_replay_kit as kit

T = TypeVar("T")

# ЗАМЕНИТЬ: адрес выдачи, признак нужного ответа API и селектор состояния страницы.
ORIGIN = "https://www.example.ru"
SEARCH_URL = ORIGIN + "/search/?text={query}&sort={sort}"
API_MARK = "/api/search"  # часть адреса запроса, который отдаёт страницу выдачи
STATE_SELECTOR = '[id^="state-searchResults"]'  # элемент с JSON выдачи в data-state или None
CAPTCHA_TITLES = ("Antibot Captcha",)  # заголовки страницы с капчей, которую не обходим

PAGE_TIMEOUT_MS = 90_000  # загрузка страницы
READY_TIMEOUT_S = 45  # проверка антибота и появление данных
NEXT_PAGE_TIMEOUT_S = 45  # следующая страница при прокрутке
ATTEMPTS = 3  # попыток открыть первую страницу
RETRY_PAUSE_S = 10  # пауза между попытками
SCROLL_PAUSE_MS = 700  # умеренная скорость прокрутки

COLUMNS = ["position", "id", "name", "price", "price_old", "rating", "reviews", "url"]


class Captcha(RuntimeError):
    """Сайт показал капчу. Её не обходим: запуск останавливается с понятной ошибкой."""


# --- разбор данных: ЗАМЕНИТЬ под структуру сайта ----------------------------------


def items_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Карточки из JSON выдачи (ответ API или data-state)."""
    return list(data.get("items") or [])


def page_number(url: str, data: dict[str, Any]) -> int:
    """Номер страницы из адреса запроса к API или из самого ответа."""
    if data.get("page"):
        return int(data["page"])
    return int(parse_qs(urlparse(url).query).get("page", ["1"])[0])


def card_row(item: dict[str, Any], position: int) -> dict[str, Any]:
    """Строка CSV из карточки. Цены в рублях; если API отдаёт копейки, делить на 100."""
    link = (item.get("url") or "").split("?")[0]  # без меток отслеживания
    return {
        "position": position,
        "id": item.get("id"),
        "name": item.get("name"),
        "price": item.get("price"),
        "price_old": item.get("price_old"),
        "rating": item.get("rating"),
        "reviews": item.get("reviews"),
        "url": ORIGIN + link if link.startswith("/") else link,
    }


# --- общие приёмы ---------------------------------------------------------------


def with_retries(what: str, fn: Callable[[], T], attempts: int = ATTEMPTS, pause: float = RETRY_PAUSE_S) -> T:
    """Повтор с паузой. Капча не повторяется: на том же выходе браузера она не уйдёт."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Captcha:
            raise
        except Exception as e:
            last = e
            if attempt < attempts:
                kit.log.warn(f"{what}, попытка {attempt}: {e}; повтор через {pause:.0f} с")
                time.sleep(pause)
    raise RuntimeError(f"{what}: не получилось за {attempts} попытки: {last}")


def wait_ready(page: Any, timeout: float = READY_TIMEOUT_S) -> list[dict[str, Any]]:
    """Ждёт, пока пройдёт проверка антибота и на странице появятся данные.

    Проверка перезагружает страницу, контекст на долю секунды пропадает: такие
    ошибки пропускаются. Капча даёт исключение Captcha.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            title, state = page.evaluate(
                """(sel) => {
                    const el = sel ? document.querySelector(sel) : null;
                    return [document.title, el ? el.dataset.state : null];
                }""",
                STATE_SELECTOR,
            )
        except Exception:
            page.wait_for_timeout(500)
            continue
        if title in CAPTCHA_TITLES:
            raise Captcha("сайт показал капчу: этот выход браузера помечен антиботом, попробуйте позже или другой geo")
        if state:
            return items_from(json.loads(state))
        page.wait_for_timeout(500)
    raise TimeoutError(f"выдача не появилась за {timeout:.0f} с")


def scroll_until(page: Any, wanted: int, pages: dict[int, list[dict[str, Any]]], timeout: float = NEXT_PAGE_TIMEOUT_S) -> list[dict[str, Any]]:
    """Крутит выдачу, пока страница не запросит у API страницу `wanted`."""
    deadline = time.monotonic() + timeout
    while wanted not in pages:
        if time.monotonic() > deadline:
            raise TimeoutError(f"страница {wanted} не загрузилась за {timeout:.0f} с прокрутки")
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(SCROLL_PAUSE_MS)
    return pages.pop(wanted)


class Collector:
    """CSV дописывается после каждой страницы: собранное не теряется при падении."""

    def __init__(self, path: Any):
        self.path = path
        self.count = 0
        self.seen: set[Any] = set()
        self._f = open(path, "w", encoding="utf-8-sig", newline="")  # BOM: Excel откроет кириллицу
        self._w = csv.DictWriter(self._f, fieldnames=COLUMNS)
        self._w.writeheader()
        self._f.flush()

    def add(self, items: list[dict[str, Any]], keep: Callable[[dict[str, Any]], bool] = lambda r: True) -> list[dict[str, Any]]:
        rows = []
        for item in items:
            key = item.get("id")
            if key is None or key in self.seen:  # реклама повторяется между страницами
                continue
            self.seen.add(key)
            row = card_row(item, self.count + 1)
            if not keep(row):
                continue
            self.count += 1
            rows.append(row)
        self._w.writerows(rows)
        self._f.flush()
        return rows

    def close(self) -> None:
        self._f.close()


# --- запуск ---------------------------------------------------------------------


def main() -> None:
    p = kit.params
    query = p.query.strip()
    csv_path = kit.output_dir / "cards.csv"
    collector = Collector(csv_path)
    keep = (lambda r: r["price"] is not None) if p.only_with_price else (lambda r: True)
    pages_done = 0
    min_price: float | None = None
    error: Exception | None = None

    kit.set_browser_limit(1)  # браузеров одновременно; каждый тратит минуты DAWG
    try:
        kit.stage("Открываем выдачу")
        with kit.browser(geo=p.geo or None) as b:
            page = b.page
            pages: dict[int, list[dict[str, Any]]] = {}

            def on_response(response: Any) -> None:
                if API_MARK not in response.url or response.status != 200:
                    return
                try:
                    data = response.json()
                    pages[page_number(response.url, data)] = items_from(data)
                except Exception:
                    pass  # чужой ответ или оборванное тело: дождёмся следующего

            page.on("response", on_response)
            url = SEARCH_URL.format(query=quote(query), sort=p.sort)

            def first() -> list[dict[str, Any]]:
                page.goto(url, timeout=PAGE_TIMEOUT_MS)
                return wait_ready(page)

            items = with_retries("первая страница", first)
            if not items:
                kit.log.warn(f"по запросу «{query}» ничего не найдено")
            kit.stage("Собираем карточки")

            for n in range(1, p.max_pages + 1):
                if n > 1:
                    items = scroll_until(page, n, pages)
                if not items:
                    break
                rows = collector.add(items, keep)
                pages_done = n
                kit.rows("cards", rows, columns=COLUMNS)
                kit.progress("pages", "Страницы выдачи", n, p.max_pages)
                kit.metric("cards", "Собрано карточек", collector.count)
                prices = [r["price"] for r in rows if r["price"] is not None]
                if prices and (min_price is None or min(prices) < min_price):
                    min_price = min(prices)
                    kit.metric("min_price", "Самая низкая цена", min_price, "₽")
    except Exception as e:  # отмена (SystemExit) сюда не попадает, её обработают finally и приложение
        error = e
        kit.log.error(f"{type(e).__name__}: {e}")
    finally:
        collector.close()
        if collector.count:
            kit.artifact(csv_path, "Карточки (CSV)", "text/csv")

    summary = f"«{query}»: {collector.count} карточек, страниц выдачи: {pages_done}"
    if error is None:
        kit.result("ok", summary)
    elif collector.count:
        kit.result("partial", f"{summary}. Остановились на ошибке: {error}")
    else:
        kit.result("failed", f"«{query}»: ничего не собрано. Ошибка: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
