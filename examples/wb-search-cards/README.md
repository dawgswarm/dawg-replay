# wb-search-cards

Эталонный скрипт по контракту v1: карточки Wildberries по поисковому запросу.
Данные берутся из ответов внутреннего API поиска, которые запрашивает сама страница
(перехват `page.expect_response` и `page.on("response")`), следующие страницы
выдачи подгружаются прокруткой. Результат: `cards.csv` в каталоге результатов.

Параметры: `query` (обязателен), `max_pages` (1–50, по 100 карточек), `sort`
(`popular`, `rate`, `priceup`, `pricedown`, `newly`), `geo` (город браузера, по
умолчанию `moskva`).

## Запуск из приложения

    dawg-replay validate examples/wb-search-cards/script.py
    dawg-replay add examples/wb-search-cards/script.py
    dawg-replay run wb-search-cards --set "query=кроссовки мужские" --set max_pages=2 --follow

## Автономно

    export DAWG_API_KEY=...
    uv run examples/wb-search-cards/script.py --query "ноутбук" --max-pages 1 --sort priceup

Пока kit не опубликован на PyPI, при разработке нужен локальный kit:

    dawg-replay config set dev_kit_source "$PWD/packages/dawg-replay-kit"   # абсолютный путь; для run из приложения
    eval "$(dawg-replay dev kit-env | head -1)"                      # для автономного запуска
    uv run --reinstall-package dawg-replay-kit examples/wb-search-cards/script.py --query "ноутбук"

Ozon как эталон не подошёл: на браузерах DAWG он сразу показывает капчу-пазл.
