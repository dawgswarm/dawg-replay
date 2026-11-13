# ozon-search-cards

Эталонный скрипт по контракту v1: карточки Ozon по поисковому запросу.
Разведка сайта: 14.11.2026.

Данные берутся из самой выдачи. Первая страница лежит в JSON атрибута `data-state` виджета
`tileGridDesktop` на странице поиска, следующие приходят перехваченными ответами
`/api/entrypoint-api.bx/page/json/v2` (страница запрашивает их при прокрутке).
Заголовки не подделываются. Результат: `cards.csv` в каталоге результатов.

Колонки: `position`, `sku`, `name`, `price`, `price_old`, `rating`, `reviews`, `url`.

Продавца нет. В выдаче Ozon продавца нет, а страница товара и запрос её данных
отдают антибот (слайдер-капчу «Сопоставьте пазл» или 403). Скрипт на страницы
товаров не ходит. Если у карточки вместо оценки товара «рейтинг магазина», `rating`
и `reviews` пустые.

Параметры: `query` (обязателен), `max_pages` (1–100; размер страницы задаёт Ozon,
при разведке 8 или 36 карточек), `sort` (`score`, `new`, `price`, `price_desc`,
`rating`, `discount`), `geo` (город браузера, по умолчанию `moskva`).

Антибот: поиск проходит JS-проверку «Antibot Challenge Page» сам за несколько секунд,
первая страница при сбое повторяется с паузой. Слайдер-капчу скрипт не обходит:
запуск останавливается с ошибкой «Ozon показал слайдер-капчу».

## Запуск из приложения

    dawg-replay validate examples/ozon-search-cards/script.py
    dawg-replay add examples/ozon-search-cards/script.py
    dawg-replay run ozon-search-cards --set "query=чайник электрический" --set sort=price --follow

## Автономно

    export DAWG_API_KEY=...
    uv run examples/ozon-search-cards/script.py --query "кроссовки мужские" --max-pages 3

Пока kit не опубликован на PyPI, при разработке нужен локальный kit:

    dawg-replay config set dev_kit_source "$PWD/packages/dawg-replay-kit"   # для run из приложения
    eval "$(dawg-replay dev kit-env | head -1)"                             # для автономного запуска
    uv run --reinstall-package dawg-replay-kit examples/ozon-search-cards/script.py --query "ноутбук"
