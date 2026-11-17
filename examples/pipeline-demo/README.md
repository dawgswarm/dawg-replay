# Демо проекта: цены WB и Ozon

Четыре скрипта-заглушки без браузера и сайта (ключ DAWG не нужен, предупреждение «ключ DAWG не задан» можно
не замечать), два встроенных узла и проект `demo-prices.project.toml` из них.

| Скрипт | Что делает | Параметры | Отдаёт (`[[outputs]]`) |
|---|---|---|---|
| `demo-wb-prices` | «собирает» цены WB | `outcome` (готово, частично, упасть, упасть один раз), `minutes` (сколько работать), `rows`, `broken` (колонка `cost` вместо `price`) | `prices.csv`: `sku`, `price` |
| `demo-ozon-prices` | то же для Ozon, цены другие | те же | `prices.csv`: `sku`, `price` |
| `demo-update-prices` | берёт меньшую цену минус 1 | `wb` (таблица, обязательная), `ozon` (таблица, необязательная), колонки `sku`, `price` | `updated.csv`: `sku`, `old_price`, `new_price` |
| `demo-report` | сводка по новым ценам | `updated` (таблица), колонки `sku`, `new_price` | `report.csv` |

Встроенные узлы «Склеить таблицы» (`std-merge-tables`) и «Если» (`std-if`) приложение ставит само.

## Схема

```
WB ──────┬─ prices.csv → wb ───┐
         │                     ├─ Обновить цены ─ updated.csv → table ─ Если: изменений больше трёх?
Ozon ────┼─ prices.csv → ozon ─┘                                          ├─ yes.csv → Отчёт
         │                                                                └─ no.csv → Отчёт: мало изменений
         └─ prices.csv → table_1, table_2 ─ Склеить таблицы «Все цены» (колонка «Площадка»)
```

Два источника идут параллельно. «Обновить цены» ждёт обоих (`wait = "all"` по умолчанию). «Все цены» склеивает
обе таблицы и добавляет колонку «Площадка». «Если» отдаёт таблицу как `yes.csv` или `no.csv`, стрелки с
`if_file` ведут каждая по своей ветке: вторая ветка в итоге «не по этой ветке», и это нормальный исход.

## Как проверить

```sh
for f in examples/pipeline-demo/demo-*.py; do dawg-replay add "$f"; done
dawg-replay project add examples/pipeline-demo/demo-prices.project.toml

dawg-replay project check demo-prices
dawg-replay project run demo-prices --follow
```

Итог «готово»: шесть шагов готово, «Отчёт: мало изменений» не по этой ветке. Холст проекта и запуск со
статусом каждого узла: `dawg-replay ui`, экран «Проекты».

Ветка «нет»: поставьте узлу «Если» `rows = 10` в `values` (в файле или на холсте) и запустите снова.

Упавший источник и перезапуск с упавшего: узлу `ozon` дайте `values = { outcome = "failed_once" }`, обновите
проект (`dawg-replay project update <файл>` или сохранение на холсте) и запустите. Ozon упадёт, «Обновить
цены», «Все цены» и дальше пропущены, итог «упал». Затем:

```sh
dawg-replay project retry <id запуска проекта> --follow
```

WB берётся готовым из прошлого запуска, Ozon идёт заново и проходит, итог «готово». Падение «один раз»
помнится файлом в каталоге `dawg-replay-demo-pipeline` во временном каталоге системы; удалите его, чтобы
Ozon снова упал.

Остановка: дайте `demo-wb-prices` время, `values = { minutes = 1 }` у узла `wb`, запустите проект и нажмите
Ctrl+C в `--follow` или `dawg-replay project stop <id>`. Идущие шаги отменяются, остальные пропускаются, итог
«отменён».

## Без проекта: ссылка на результат

Файл результата одного скрипта подставляется в параметр другого и без проекта:

```sh
dawg-replay run demo-wb-prices --follow
dawg-replay run demo-ozon-prices --follow
dawg-replay run demo-update-prices --set wb=last:demo-wb-prices:prices.csv --set ozon=last:demo-ozon-prices:prices.csv --follow
dawg-replay run demo-ozon-prices --set broken=true --follow
dawg-replay run demo-update-prices --set wb=last:demo-wb-prices:prices.csv --set ozon=last:demo-ozon-prices:prices.csv --follow
```

Последний запуск не начнётся: в `prices.csv` Ozon нет колонки `price`, причина видна в истории.

Проход по строкам и значения по стрелке: демо `examples/project-rows-demo`.

Удалить демо: `dawg-replay project remove demo-prices` и `dawg-replay remove <скрипт>` для каждого скрипта.
