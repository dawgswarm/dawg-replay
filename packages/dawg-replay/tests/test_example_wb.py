"""Эталонный скрипт examples/wb-search-cards: манифест и разбор ответа API без браузера."""

import csv
import importlib.util
from pathlib import Path

import pytest
from dawg_replay_kit.manifest import load_manifest

SCRIPT = Path(__file__).resolve().parents[3] / "examples" / "wb-search-cards" / "script.py"

PRODUCT = {
    "id": 394213147,
    "name": "Кроссовки DROPSET CONTROL TRA",
    "brand": "adidas",
    "supplier": "Wildberries",
    "supplierRating": 4.8,
    "reviewRating": 4.8,
    "feedbacks": 706,
    "sizes": [
        {"name": "38", "price": {"basic": 1239900, "product": 719100}},
        {"name": "39", "price": {"basic": 1239900, "product": 749100}},
        {"name": "40", "price": {}},  # нет в наличии
    ],
}


@pytest.fixture(scope="module")
def wb():
    spec = importlib.util.spec_from_file_location("wb_search_cards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # kit поднимается лениво, импорт ничего не запускает
    return module


def test_manifest_valid():
    m = load_manifest(SCRIPT)
    assert m.name == "wb-search-cards"
    assert [p.name for p in m.params] == ["query", "max_pages", "sort", "geo"]
    assert "dawg-replay-kit>=1,<2" in m.dependencies


def test_card_row_takes_cheapest_size_in_rubles(wb):
    row = wb.card_row(PRODUCT, 37)
    assert row == {
        "position": 37,
        "id": 394213147,
        "name": "Кроссовки DROPSET CONTROL TRA",
        "brand": "adidas",
        "price": 7191.0,
        "price_old": 12399.0,
        "rating": 4.8,
        "feedbacks": 706,
        "supplier": "Wildberries",
        "supplier_rating": 4.8,
        "url": "https://www.wildberries.ru/catalog/394213147/detail.aspx",
    }
    assert list(row) == wb.COLUMNS


def test_card_without_prices(wb):
    row = wb.card_row({"id": 1, "name": "x", "sizes": []}, 1)
    assert row["price"] is None and row["price_old"] is None and row["brand"] == ""


def test_page_number(wb):
    base = "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search?query=x&resultset=catalog"
    assert wb.page_number(base) == 1
    assert wb.page_number(base + "&page=3") == 3


def test_collector_dedups_and_writes_excel_csv(wb, tmp_path):
    path = tmp_path / "cards.csv"
    c = wb.Collector(path)
    assert len(c.add([PRODUCT, {**PRODUCT, "id": 2}])) == 2
    assert len(c.add([PRODUCT, {**PRODUCT, "id": 3}])) == 1  # повтор с прошлой страницы пропущен
    c.close()
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert [(r["position"], r["id"]) for r in rows] == [("1", "394213147"), ("2", "2"), ("3", "3")]
