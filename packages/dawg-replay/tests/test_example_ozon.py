"""Эталонный скрипт examples/ozon-search-cards: манифест и разбор выдачи без браузера.

Карточки повторяют структуру виджета tileGridDesktop, снятую при разведке 14.11.2026.
"""

import csv
import importlib.util
import json
from pathlib import Path

import pytest
from dawg_replay_kit.manifest import load_manifest

SCRIPT = Path(__file__).resolve().parents[3] / "examples" / "ozon-search-cards" / "script.py"


def tile(sku, price, old=None, name="Кроссовки", labels=(("3.7", "15 отзывов"),)):
    prices = [{"text": f"{price} ₽", "textStyle": "PRICE"}]
    if old:
        prices.append({"text": f"{old} ₽", "textStyle": "ORIGINAL_PRICE"})
    state = [
        {"type": "priceV2", "priceV2": {"price": prices, "discount": "−76%"}},
        {"type": "textDS", "textDS": {"text": "78 ед осталось"}},
        {"type": "textDS", "textDS": {"text": name, "testInfo": {"automatizationId": "tile-name"}}, "id": "name"},
    ]
    for group in labels:
        items = [{"type": "icon", "icon": {}}]
        items += [{"type": "text", "text": {"text": t}} for t in group]
        state.append({"type": "labelListV2", "labelListV2": {"items": items}})
    return {"sku": sku, "mainState": state, "action": {"link": f"/product/krossovki-{sku}/?at=track"}}


@pytest.fixture(scope="module")
def oz():
    spec = importlib.util.spec_from_file_location("ozon_search_cards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_valid():
    m = load_manifest(SCRIPT)
    assert m.name == "ozon-search-cards"
    assert (m.contract, m.contract_minor) == (1, 1) and m.site == "Ozon" and m.tags
    assert [p.name for p in m.params] == ["query", "max_pages", "sort", "geo"]
    assert "продавца нет" in m.description.lower()


def test_no_seller_column(oz):
    assert oz.COLUMNS == ["position", "sku", "name", "price", "price_old", "rating", "reviews", "url"]


def test_card_row(oz):
    row = oz.card_row(tile(4196220504, "1 514", old="26 000", labels=(("Стало дешевле",), ("4.8", "1 379 отзывов"))), 1)
    assert row == {
        "position": 1,
        "sku": 4196220504,
        "name": "Кроссовки",
        "price": 1514,
        "price_old": 26000,
        "rating": 4.8,
        "reviews": 1379,
        "url": "https://www.ozon.ru/product/krossovki-4196220504/",
    }


def test_shop_rating_is_not_product_rating(oz):
    row = oz.card_row(tile(5673102885, 362, old=1502, labels=(("4.5", "рейтинг магазина"),)), 3)
    assert row["rating"] is None and row["reviews"] is None
    assert row["price"] == 362 and row["price_old"] == 1502


def test_card_without_old_price_and_reviews(oz):
    row = oz.card_row(tile(1, 361, labels=(("Бренд проверен",),)), 2)
    assert (row["price_old"], row["rating"], row["reviews"]) == (None, None, None)
    row = oz.card_row(tile(2, 400, labels=(("5", "1 отзыв"),)), 3)
    assert (row["rating"], row["reviews"]) == (5.0, 1)


def test_grid_from_page_json(oz):
    data = {
        "widgetStates": {
            "separator-1-default-3": "{}",
            "tileGridDesktop-3669724-default-3": json.dumps({"items": [tile(1, 100), tile(2, 200)]}),
        },
        "pageInfo": {"url": "/category/kedy-7660/?layout_page_index=3&page=3&text=x"},
    }
    page, items = oz.grid_from_page_json(data)
    assert page == 3 and [i["sku"] for i in items] == [1, 2]
    assert oz.grid_from_page_json({"widgetStates": {"cookieBubble-1": "{}"}}) is None


def test_collector_dedups_and_writes_excel_csv(oz, tmp_path):
    path = tmp_path / "cards.csv"
    c = oz.Collector(path)
    assert len(c.add([tile(1, 100), tile(2, 200)])) == 2
    assert len(c.add([tile(2, 200), tile(3, 300), {"mainState": []}])) == 1  # повтор и карточка без sku
    c.close()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert [(r["position"], r["sku"], r["price"]) for r in rows] == [("1", "1", "100"), ("2", "2", "200"), ("3", "3", "300")]
