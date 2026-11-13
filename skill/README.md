# dawg-replay-builder

Скилл для кодинг-агента (Claude Code или Codex). Агент один раз проходит нужный путь по сайту в облачном браузере DAWG и собирает из него скрипт для приложения `dawg-replay`. Дальше скрипт запускается в приложении без агента.

Что внутри:

```
.claude-plugin/plugin.json        плагин Claude Code
.claude-plugin/marketplace.json   маркетплейс из одного плагина
.mcp.json                         сервер dawg-mcp (браузер DAWG для разведки)
skills/dawg-replay-builder/       сам скилл: SKILL.md и справочные файлы
```

## Что нужно заранее

1. `uv`: https://docs.astral.sh/uv/getting-started/installation/
2. Приложение: `uv tool install dawg-replay`, затем `dawg-replay config set-key` (ключ DAWG в системное хранилище паролей). Проверка: `dawg-replay --version --json` показывает `"contracts": [1]`.
3. Ключ DAWG из личного кабинета https://dawgswarm.ru.

## Claude Code

```
claude plugin marketplace add dawgswarm/dawg-replay-builder
claude plugin install dawg-replay-builder@dawg-replay
```

Затем в сессии Claude Code: `/plugin configure dawg-replay-builder` и введите ключ DAWG. Ключ хранится в системном хранилище паролей, в файлы настроек не попадает. Если устанавливать через `/plugin` внутри сессии, окно ввода ключа откроется само.

Проверка: `/mcp` показывает сервер `dawg` плагина в статусе connected, `/plugin` показывает плагин без ошибок. Скилл срабатывает сам на просьбы вроде «собирай с сайта...», вызвать явно: `/dawg-replay-builder:dawg-replay-builder`.

Если dawg-mcp у вас уже подключён вручную (`claude mcp add dawg ...`), после установки плагина будет два сервера с одинаковыми инструментами. Оставьте один:

- убрать ручной: `claude mcp remove dawg` (с тем же `--scope`, с которым добавляли), ключ тогда берётся из настройки плагина;
- или оставить ручной и отключить сервер плагина в `/mcp`.

Скиллу всё равно, какой сервер остался: он ищет инструменты `browser_*` с любым префиксом.

Локальная проверка до публикации, из корня репозитория `dawg_replay`:

```
claude plugin validate ./skill
claude --plugin-dir ./skill
```

## Codex

1. Скопируйте папку скилла в каталог скиллов пользователя:

   ```
   mkdir -p ~/.agents/skills
   cp -R skills/dawg-replay-builder ~/.agents/skills/
   ```

   Для одного проекта вместо этого: `<проект>/.agents/skills/dawg-replay-builder`.

2. Подключите dawg-mcp в `~/.codex/config.toml`:

   ```toml
   [mcp_servers.dawg]
   command = "uvx"
   args = ["dawg-mcp"]
   env_vars = ["DAWG_API_KEY"]
   ```

   `env_vars` передаёт серверу `DAWG_API_KEY` из окружения, в котором запущен Codex: перед запуском выполните `export DAWG_API_KEY=...` (или пропишите в профиль оболочки). Если dawg-mcp уже настроен (`codex mcp add dawg ...` или блок с `[mcp_servers.dawg.env]`), второй блок не добавляйте.

3. Перезапустите Codex. Проверка: `/skills` показывает `dawg-replay-builder`, `/mcp` показывает сервер `dawg`. Вызвать явно: `$dawg-replay-builder`.

## Обновление контракта

`skills/dawg-replay-builder/contract.md` это копия `docs/contract.md` из репозитория `dawg-replay`; тест `test_skill.py` следит, чтобы они совпадали. После правки контракта скопируйте файл заново.
