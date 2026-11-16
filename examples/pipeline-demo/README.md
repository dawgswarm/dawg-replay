# Демо связей между скриптами

Шесть скриптов-заглушек без браузера и сайта: минуты DAWG не тратят, ключ DAWG не нужен (предупреждение «ключ DAWG
не задан» при запуске можно не замечать). На них проверяются связи M6: цепочка, ожидание всех источников со
сроком, несовпадение колонок и круг с пределом. План приёмки: [`docs/acceptance/m6-pipelines.md`](../../docs/acceptance/m6-pipelines.md).

| Скрипт | Что делает | Параметры | Отдаёт (`[[outputs]]`) |
|---|---|---|---|
| `demo-wb-prices` | «собирает» цены WB | `outcome` (готово, частично, упасть), `minutes` (сколько работать), `rows`, `broken` (колонка `cost` вместо `price`) | `prices.csv`: `sku`, `price` |
| `demo-ozon-prices` | то же для Ozon, цены другие | те же | `prices.csv`: `sku`, `price` |
| `demo-update-prices` | берёт меньшую цену минус 1 | `wb` (таблица, обязательная), `ozon` (таблица, необязательная), колонки `sku`, `price` | `updated.csv`: `sku`, `old_price`, `new_price` |
| `demo-report` | сводка по новым ценам | `updated` (таблица), колонки `sku`, `new_price` | `report.csv` |
| `demo-ping`, `demo-pong` | перекидывают «мяч» со счётчиком | `ball` (файл, необязательный) | `ball.txt` |

## Быстрый старт

```sh
for f in examples/pipeline-demo/demo-*.py; do dawg-replay add "$f"; done

dawg-replay link add demo-wb-prices demo-update-prices --file prices.csv=wb
dawg-replay link add demo-ozon-prices demo-update-prices --file prices.csv=ozon
dawg-replay link add demo-update-prices demo-report --file @main=updated
dawg-replay link mode demo-update-prices all --wait 5m --on-timeout skip
dawg-replay link check

dawg-replay run demo-wb-prices --follow
dawg-replay run demo-ozon-prices --follow
dawg-replay runs demo-report          # отчёт запустился сам после обновления
```

Откройте `dawg-replay ui`, экран «Связи»: там схема из четырёх скриптов.

Без связей результат подставляется ссылкой:

```sh
dawg-replay run demo-update-prices --set wb=last:demo-wb-prices:prices.csv --set ozon=last:demo-ozon-prices:prices.csv --follow
```

Удалить демо: `dawg-replay remove <скрипт>` для каждого, связи удаляются вместе со скриптами.
