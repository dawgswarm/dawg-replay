# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///

# /// dawg-script
# contract = 1
# name = "ozon-search-cards"
# title = "Ozon: карточки по поисковому запросу"
# description = "Обходит выдачу Ozon по запросу и собирает название, цену, старую цену, рейтинг и число отзывов. Продавца нет: в выдаче Ozon его нет, а страница товара отдаёт капчу, поэтому скрипт на страницы товаров не ходит."
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
# title = "Сколько страниц выдачи обойти"
# description = "Размер страницы задаёт Ozon: при разведке было от 8 до 36 карточек"
# default = 5
# min = 1
# max = 100
#
# [[params]]
# name = "sort"
# type = "enum"
# title = "Сортировка"
# description = "score: популярные, new: новинки, price: дешевле, price_desc: дороже, rating: с высоким рейтингом, discount: с большими скидками"
# choices = ["score", "new", "price", "price_desc", "rating", "discount"]
# default = "score"
#
# [[params]]
# name = "geo"
# type = "string"
# title = "Город браузера"
# description = "Slug города DAWG: от него зависят цены и наличие. Если пусто, браузер без привязки к городу"
# default = "moskva"
# ///
"""Карточки Ozon по поисковому запросу.

Разведка сайта: 14.11.2026. Если Ozon поменял устройство выдачи, начинать с неё.

- Поиск `/search/?text=...` проходит JS-проверку «Antibot Challenge Page» сам за
  несколько секунд и обычно перенаправляет в категорию с тем же `text`.
- Первая страница выдачи лежит в самой странице: JSON в `data-state` элемента
  `state-tileGridDesktop-*`. Следующие страницы (`page=N`) страница запрашивает
  при прокрутке из `/api/entrypoint-api.bx/page/json/v2?url=...`; скрипт
  перехватывает эти ответы, заголовки не подделывает. Размер страницы задаёт Ozon
  (при разведке 8 или 36 карточек, в зависимости от выдачи).
- Продавца в выдаче нет. Страница товара и запрос её данных отдают антибот
  (слайдер-капчу или 403), поэтому скрипт на страницы товаров не ходит и колонки
  продавца нет. Капчу скрипт не обходит: если она появилась, запуск останавливается
  с понятной ошибкой.

Результат: `cards.csv` (UTF-8 с BOM, открывается в Excel), строки дописываются
после каждой страницы. При падении на середине собранное остаётся, итог `partial`.

Колонки: `position`, `sku`, `name`, `price` (цена на карточке, ₽), `price_old`
(зачёркнутая цена), `rating` и `reviews` (оценка товара и число отзывов; пусто,
если на карточке вместо них «рейтинг магазина» или товар без отзывов), `url`.
"""

from __future__ import annotations

import csv
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import dawg_replay_kit as kit

ORIGIN = "https://www.ozon.ru"
GRID_PREFIX = "tileGridDesktop"
FIRST_PAGE_ATTEMPTS = 3
FIRST_PAGE_PAUSE_S = 10  # пауза перед повтором первой страницы
CHALLENGE_TIMEOUT_S = 45  # сколько ждать прохождения JS-проверки и появления выдачи
SCROLL_TIMEOUT_S = 45

COLUMNS = ["position", "sku", "name", "price", "price_old", "rating", "reviews", "url"]

_RATING = re.compile(r"^\d(?:[.,]\d+)?$")
_REVIEWS = re.compile(r"^([\d\s  ]+)\s*отзыв")


class Captcha(RuntimeError):
    """Ozon показал слайдер-капчу. Её не обходим."""


def rubles(text: str | None) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def number(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def card_row(item: dict[str, Any], position: int) -> dict[str, Any]:
    """Карточка выдачи (элемент `items` виджета tileGridDesktop) в строку CSV."""
    name = price = price_old = rating = reviews = None
    for state in item.get("mainState") or []:
        kind = state.get("type")
        if kind == "priceV2":
            for p in (state.get("priceV2") or {}).get("price") or []:
                if p.get("textStyle") == "PRICE":
                    price = rubles(p.get("text"))
                elif p.get("textStyle") == "ORIGINAL_PRICE":
                    price_old = rubles(p.get("text"))
        elif kind == "textDS":
            text_ds = state.get("textDS") or {}
            auto = ((text_ds.get("testInfo") or {}).get("automatizationId")) or ""
            if state.get("id") == "name" or auto == "tile-name":
                name = text_ds.get("text")
        elif kind == "labelListV2":
            texts = [
                ((label.get("text") or {}).get("text") or "").strip()
                for label in (state.get("labelListV2") or {}).get("items") or []
            ]
            # «4.5 рейтинг магазина» это оценка продавца, а не товара, её не берём.
            if any("магазин" in t for t in texts):
                continue
            for text in texts:
                if rating is None and _RATING.match(text):
                    rating = float(text.replace(",", "."))
                m = _REVIEWS.match(text)
                if reviews is None and m:
                    reviews = number(m.group(1))
    link = ((item.get("action") or {}).get("link") or "").split("?")[0]
    sku = item.get("sku")
    return {
        "position": position,
        "sku": sku,
        "name": name,
        "price": price,
        "price_old": price_old,
        "rating": rating,
        "reviews": reviews,
        "url": ORIGIN + link if link else f"{ORIGIN}/product/{sku}/",
    }


def grid_from_page_json(data: dict[str, Any]) -> tuple[int, list[dict]] | None:
    """Страница выдачи из ответа entrypoint-api: (номер страницы, карточки) или None."""
    states = data.get("widgetStates") or {}
    key = next((k for k in states if k.startswith(GRID_PREFIX)), None)
    if key is None:
        return None
    grid = json.loads(states[key])
    url = (data.get("pageInfo") or {}).get("url") or ""
    page = parse_qs(urlparse(url).query).get("page", ["1"])[0]
    return int(page), grid.get("items") or []


def is_page_response(response: Any) -> bool:
    return "/api/entrypoint-api.bx/page/json/v2" in response.url and response.status == 200


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

    def add(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for item in items:
            sku = item.get("sku")
            if sku is None or sku in self.seen:  # рекламные карточки повторяются между страницами
                continue
            self.seen.add(sku)
            self.count += 1
            rows.append(card_row(item, self.count))
        self._w.writerows(rows)
        self._f.flush()
        return rows

    def close(self) -> None:
        self._f.close()


def open_first_page(page: Any, url: str) -> list[dict]:
    """Открывает выдачу и ждёт, пока пройдёт JS-проверка. Повторяет с паузой."""
    last: Exception | None = None
    for attempt in range(1, FIRST_PAGE_ATTEMPTS + 1):
        try:
            page.goto(url, timeout=90_000)
            deadline = time.monotonic() + CHALLENGE_TIMEOUT_S
            while time.monotonic() < deadline:
                try:
                    title, state = page.evaluate(
                        f"""() => {{
                            const el = document.querySelector('[id^="state-{GRID_PREFIX}"]');
                            return [document.title, el ? el.dataset.state : null];
                        }}"""
                    )
                except Exception:
                    # JS-проверка перезагружает страницу: контекст пропадает на долю секунды.
                    page.wait_for_timeout(500)
                    continue
                if title == "Antibot Captcha":
                    raise Captcha("Ozon показал слайдер-капчу «Сопоставьте пазл»: этот выход браузера помечен антиботом")
                if state:
                    return json.loads(state).get("items") or []
                page.wait_for_timeout(500)
            raise TimeoutError(f"выдача не появилась за {CHALLENGE_TIMEOUT_S} с")
        except Captcha:
            raise
        except Exception as e:
            last = e
            if attempt < FIRST_PAGE_ATTEMPTS:
                kit.log.warn(f"первая страница, попытка {attempt}: {e}; повтор через {FIRST_PAGE_PAUSE_S} с")
                time.sleep(FIRST_PAGE_PAUSE_S)
    raise RuntimeError(f"первая страница не открылась за {FIRST_PAGE_ATTEMPTS} попытки: {last}")


def next_page(page: Any, wanted: int, pages: dict[int, list[dict]]) -> list[dict]:
    """Крутит выдачу, пока страница не запросит `page=wanted`."""
    deadline = time.monotonic() + SCROLL_TIMEOUT_S
    while wanted not in pages:
        if time.monotonic() > deadline:
            raise TimeoutError(f"страница {wanted} выдачи не загрузилась за {SCROLL_TIMEOUT_S} с прокрутки")
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(700)
    return pages.pop(wanted)


def main() -> None:
    p = kit.params
    query, max_pages = p.query.strip(), p.max_pages
    csv_path = kit.output_dir / "cards.csv"
    collector = Collector(csv_path)
    pages_done = 0
    min_price: int | None = None
    error: BaseException | None = None

    try:
        kit.stage("Открываем выдачу")
        with kit.browser(geo=p.geo or None) as b:
            page = b.page
            pages: dict[int, list[dict]] = {}

            def on_response(response: Any) -> None:
                if not is_page_response(response):
                    return
                try:
                    found = grid_from_page_json(response.json())
                except Exception:
                    return  # не наш виджет или ответ оборвался
                if found and found[0] > 1:
                    pages[found[0]] = found[1]

            page.on("response", on_response)
            url = f"{ORIGIN}/search/?text={quote(query)}&from_global=true&sorting={p.sort}"
            items = open_first_page(page, url)
            if not items:
                kit.log.warn(f"по запросу «{query}» ничего не найдено")
            kit.stage("Собираем карточки")

            for n in range(1, max_pages + 1):
                if n > 1:
                    items = next_page(page, n, pages)
                if not items:
                    break
                rows = collector.add(items)
                pages_done = n
                kit.rows("cards", rows, columns=COLUMNS)
                kit.progress("pages", "Страницы выдачи", n, max_pages)
                kit.metric("cards", "Собрано карточек", collector.count)
                prices = [r["price"] for r in rows if r["price"]]
                if prices and (min_price is None or min(prices) < min_price):
                    min_price = min(prices)
                    kit.metric("min_price", "Самая низкая цена", min_price, "₽")
    except Exception as e:  # SystemExit (отмена) сюда не попадает: им займутся finally и приложение
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
