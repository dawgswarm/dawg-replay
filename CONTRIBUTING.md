# Разработка dawg-replay

Устройство и требования: [техническое задание](docs/specs/2026-11-13-dawg-replay-design.md) и [контракт скрипта](docs/contract.md).

## Состав репозитория

```
packages/dawg-replay       приложение: ядро, CLI, веб-интерфейс
packages/dawg-replay-kit   библиотека для скриптов: параметры, события, браузеры DAWG
skill/                     плагин dawg-replay-builder (скилл для агента и конфиг dawg-mcp)
examples/                  эталонные скрипты Ozon и Wildberries
docs/                      контракт скрипта, ТЗ, приёмка этапов
```

## Окружение и тесты

Поддерживается Python 3.10 и новее. Окружение разработки по умолчанию собирается на нижней границе: в корне лежит `.python-version` с `3.10`, поэтому `uv sync` и `uv run` берут Python 3.10 (uv скачает его сам, если нужно).

```sh
uv sync                                        # окружение .venv на Python 3.10
uv run pytest                                  # тесты на 3.10
uv run --isolated --python 3.14 pytest         # тесты на новейшем Python во временном окружении, .venv не меняется
```

Перед сдачей изменений тесты должны проходить в обоих вариантах. Синтаксис новее 3.10 (например, `\` внутри выражений f-строк) на 3.10 не работает.

## Приложение из репозитория

`uv run dawg-replay ...` запускает приложение из рабочей копии. Чтобы не смешивать проверки со своими скриптами, задайте отдельный каталог данных:

```sh
export DAWG_REPLAY_HOME=~/dawg-replay-dev
```

Скрипты ставят `dawg-replay-kit` с PyPI. Чтобы скрипты брали kit из репозитория (до публикации или при правках kit), укажите путь к нему:

```sh
uv run dawg-replay config set dev_kit_source "$PWD/packages/dawg-replay-kit"
```

Приложение соберёт wheel kit и подложит его в `uv run` скрипта, подробности в `dawg_replay/devkit.py`. Пустое значение возвращает обычное поведение.

## Плагин

Проверка плагина из рабочей копии, из корня репозитория:

```sh
claude plugin validate ./skill
claude --plugin-dir ./skill
```

Для Codex папку `skill/skills/dawg-replay-builder` можно подключить симлинком в `~/.agents/skills/`.

`skill/skills/dawg-replay-builder/contract.md` это копия `docs/contract.md`; тест `test_skill.py` следит, чтобы они совпадали. После правки контракта скопируйте файл заново.
