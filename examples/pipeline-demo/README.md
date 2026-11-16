# Демо: результат одного скрипта на входе другого

Шесть скриптов-заглушек без браузера и сайта: минуты DAWG не тратят, ключ DAWG не нужен (предупреждение «ключ DAWG
не задан» при запуске можно не замечать). На них проверяется передача файла результата в параметр другого скрипта
и несовпадение колонок.

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

dawg-replay run demo-wb-prices --follow
dawg-replay run demo-ozon-prices --follow
dawg-replay run demo-update-prices --set wb=last:demo-wb-prices:prices.csv --set ozon=last:demo-ozon-prices:prices.csv --follow
dawg-replay run demo-report --set updated=last:demo-update-prices:@main --follow
```

Удалить демо: `dawg-replay remove <скрипт>` для каждого.
