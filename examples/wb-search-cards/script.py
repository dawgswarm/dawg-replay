# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///

# /// dawg-script
# contract = 1
# name = "wb-search-cards"
# title = "Wildberries: карточки по поисковому запросу"
# description = "Обходит выдачу Wildberries по запросу и собирает название, бренд, цену, рейтинг, число оценок и продавца."
# version = 2
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
# title = "Сколько страниц выдачи обойти (по 100 карточек)"
# default = 3
# min = 1
# max = 50
#
# [[params]]
# name = "sort"
# type = "enum"
# title = "Сортировка"
# description = "popular — по популярности, rate — по рейтингу, priceup и pricedown — по цене, newly — новинки"
# choices = ["popular", "rate", "priceup", "pricedown", "newly"]
# default = "popular"
#
# [[params]]
# name = "geo"
# type = "string"
# title = "Город браузера"
# description = "Slug города DAWG: от него зависят цены и наличие. Пусто — без привязки"
# default = "moskva"
# ///
"""Карточки Wildberries по поисковому запросу.

Данные берутся не из вёрстки, а из ответов внутреннего API поиска, которые
запрашивает сама страница (`/__internal/u-search/.../search?...&resultset=catalog`).
Первая страница приходит при открытии выдачи, следующие (`page=N`) при прокрутке.
Скрипт перехватывает эти ответы, поэтому не подделывает заголовки и не зависит
от разметки карточек. Цены в API в копейках.

Колонки: `price` — цена на карточке выдачи (самый дешёвый размер), без
персональной скидки WB Кошелька, которую страница товара показывает крупно;
`price_old` — зачёркнутая цена; `rating` и `feedbacks` — оценка и число оценок
товара; `supplier` и `supplier_rating` — продавец и его рейтинг.

Результат: `cards.csv` (UTF-8 с BOM, открывается в Excel). Строки дописываются
после каждой страницы, поэтому собранное не теряется при падении на середине:
тогда итог `partial`.
"""

from __future__ import annotations

import csv
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import dawg_replay_kit as kit

PAGE_SIZE = 100
RESPONSE_TIMEOUT_MS = 90_000  # первая страница: браузер, проверка WB, загрузка выдачи
SCROLL_TIMEOUT_S = 45  # следующая страница: сколько крутить, пока не придёт ответ

COLUMNS = [
    "position",
    "id",
    "name",
    "brand",
    "price",
    "price_old",
    "rating",
    "feedbacks",
    "supplier",
    "supplier_rating",
    "url",
]


def is_catalog_response(response: Any) -> bool:
    return "/u-search/exactmatch/" in response.url and "resultset=catalog" in response.url


def page_number(url: str) -> int:
    values = parse_qs(urlparse(url).query).get("page")
    return int(values[0]) if values else 1


def rub(kopecks: Any) -> float | None:
    return round(kopecks / 100, 2) if isinstance(kopecks, (int, float)) else None


def card_row(product: dict[str, Any], position: int) -> dict[str, Any]:
    prices = [s.get("price") or {} for s in product.get("sizes") or []]
    current = [p["product"] for p in prices if p.get("product")]
    basic = [p["basic"] for p in prices if p.get("basic")]
    return {
        "position": position,
        "id": product.get("id"),
        "name": product.get("name"),
        "brand": product.get("brand") or "",
        "price": rub(min(current)) if current else None,  # самый дешёвый размер, как на карточке
        "price_old": rub(min(basic)) if basic else None,
        "rating": product.get("reviewRating"),
        "feedbacks": product.get("feedbacks"),
        "supplier": product.get("supplier") or "",
        "supplier_rating": product.get("supplierRating"),
        "url": f"https://www.wildberries.ru/catalog/{product.get('id')}/detail.aspx",
    }


class Collector:
    """Пишет строки в CSV сразу и считает, что собрано."""

    def __init__(self, path):
        self.path = path
        self.count = 0
        self.seen: set[Any] = set()
        self._f = open(path, "w", encoding="utf-8-sig", newline="")
        self._w = csv.DictWriter(self._f, fieldnames=COLUMNS)
        self._w.writeheader()
        self._f.flush()

    def add(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for p in products:
            if p.get("id") in self.seen:  # рекламные карточки могут повторяться между страницами
                continue
            self.seen.add(p.get("id"))
            self.count += 1
            rows.append(card_row(p, self.count))
        self._w.writerows(rows)
        self._f.flush()
        return rows

    def close(self) -> None:
        self._f.close()


def next_page(page: Any, wanted: int, responses: dict[int, dict]) -> dict:
    """Крутит выдачу, пока страница не запросит `page=wanted`."""
    deadline = time.monotonic() + SCROLL_TIMEOUT_S
    while wanted not in responses:
        if time.monotonic() > deadline:
            raise TimeoutError(f"страница {wanted} выдачи не загрузилась за {SCROLL_TIMEOUT_S} с прокрутки")
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(700)
    return responses.pop(wanted)


def main() -> None:
    p = kit.params
    query, max_pages = p.query.strip(), p.max_pages
    csv_path = kit.output_dir / "cards.csv"
    collector = Collector(csv_path)
    total_found: int | None = None
    pages_done = 0
    min_price: float | None = None
    error: BaseException | None = None

    try:
        kit.stage("Открываем выдачу")
        with kit.browser(geo=p.geo or None) as b:
            page = b.page
            responses: dict[int, dict] = {}

            def on_response(response: Any) -> None:
                if not is_catalog_response(response) or response.status != 200:
                    return
                try:
                    responses[page_number(response.url)] = response.json()
                except Exception:
                    pass  # страница не закончила ответ: дождёмся повтора или таймаута

            url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote(query)}&sort={p.sort}"
            with page.expect_response(
                lambda r: is_catalog_response(r) and page_number(r.url) == 1, timeout=RESPONSE_TIMEOUT_MS
            ) as first:
                page.goto(url, timeout=RESPONSE_TIMEOUT_MS)
            data = first.value.json()
            page.on("response", on_response)

            total_found = int(data.get("total") or 0)
            kit.metric("found", "Найдено на WB", total_found, "товаров")
            if total_found == 0:
                kit.log.warn(f"по запросу «{query}» ничего не найдено")
            pages_total = min(max_pages, max(1, -(-total_found // PAGE_SIZE)))
            kit.stage("Собираем карточки")

            for n in range(1, pages_total + 1):
                if n > 1:
                    data = next_page(page, n, responses)
                products = data.get("products") or []
                if not products:
                    break
                rows = collector.add(products)
                pages_done = n
                kit.rows("cards", rows, columns=COLUMNS)
                kit.progress("pages", "Страницы выдачи", n, pages_total)
                kit.metric("cards", "Собрано карточек", collector.count)
                prices = [r["price"] for r in rows if r["price"]]
                if prices and (min_price is None or min(prices) < min_price):
                    min_price = min(prices)
                    kit.metric("min_price", "Самая низкая цена", min_price, "₽")
                if len(products) < PAGE_SIZE:
                    break
    except Exception as e:  # SystemExit (отмена) сюда не попадает: им займутся finally и приложение
        error = e
        kit.log.error(f"{type(e).__name__}: {e}")
    finally:
        collector.close()
        if collector.count:
            kit.artifact(csv_path, "Карточки (CSV)", "text/csv")

    summary = f"«{query}»: {collector.count} карточек, страниц выдачи: {pages_done}"
    if total_found is not None:
        summary += f" (всего на WB {total_found})"
    if error is None:
        kit.result("ok", summary)
    elif collector.count:
        kit.result("partial", f"{summary}. Остановились на ошибке: {error}")
    else:
        kit.result("failed", f"«{query}»: ничего не собрано. Ошибка: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
