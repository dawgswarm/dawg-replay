# dawg-replay-builder

Скилл для кодинг-агента (Claude Code или Codex). Агент один раз проходит нужный путь по сайту в облачном браузере DAWG и собирает из него скрипт для приложения `dawg-replay`. Дальше скрипт запускается в приложении без агента.

Что внутри:

```
.claude-plugin/plugin.json        плагин Claude Code
.mcp.json                         сервер dawg-mcp (браузер DAWG для разведки)
skills/dawg-replay-builder/       сам скилл: SKILL.md и справочные файлы
```

## Что нужно заранее

1. `uv`: https://docs.astral.sh/uv/getting-started/installation/
2. Приложение: `uv tool install dawg-replay`, затем `dawg-replay config set-key` (ключ DAWG в системное хранилище паролей). Проверка: `dawg-replay --version --json` показывает `"contract_versions": ["1.2"]`.
3. Ключ DAWG из личного кабинета https://dawgswarm.ru.

## Рабочая папка

Заведите одну папку для работы с агентом, например `~/dawg-scripts`, и открывайте Claude Code (или Codex) в ней:

```
mkdir -p ~/dawg-scripts
cd ~/dawg-scripts
claude
```

Агент кладёт черновик каждого скрипта в свою подпапку, `~/dawg-scripts/<name>/script.py`, так что несколько скриптов не мешают друг другу. После `dawg-replay add` приложение хранит свою копию скрипта; черновик можно удалить или оставить для следующей правки.

## Claude Code

В сессии Claude Code:

```
/plugin marketplace add dawgswarm/dawg-replay
/plugin install dawg-replay-builder@dawg-replay
```

При установке откроется окно ввода ключа DAWG. Ключ хранится в системном хранилище паролей, в файлы настроек не попадает. То же из терминала: `claude plugin marketplace add dawgswarm/dawg-replay` и `claude plugin install dawg-replay-builder@dawg-replay`, тогда ключ вводится в сессии командой `/plugin configure dawg-replay-builder`.

Проверка: `/mcp` показывает сервер `dawg` плагина в статусе connected, `/plugin` показывает плагин без ошибок. Скилл срабатывает сам на просьбы вроде «собирай с сайта...», вызвать явно: `/dawg-replay-builder:dawg-replay-builder`.

Если dawg-mcp у вас уже подключён вручную (`claude mcp add dawg ...`), после установки плагина будет два сервера с одинаковыми инструментами. Оставьте один:

- убрать ручной: `claude mcp remove dawg` (с тем же `--scope`, с которым добавляли), ключ тогда берётся из настройки плагина;
- или оставить ручной и отключить сервер плагина в `/mcp`.

Скиллу всё равно, какой сервер остался: он ищет инструменты `browser_*` с любым префиксом.

Обновление: `/plugin marketplace update dawg-replay`.

## Codex

1. Скачайте репозиторий и скопируйте папку скилла в каталог скиллов пользователя:

   ```
   git clone --depth 1 https://github.com/dawgswarm/dawg-replay ~/dawg-replay
   mkdir -p ~/.agents/skills
   cp -R ~/dawg-replay/skill/skills/dawg-replay-builder ~/.agents/skills/
   ```

   Чтобы не качать весь репозиторий, можно взять только `skill/`:

   ```
   git clone --depth 1 --filter=blob:none --sparse https://github.com/dawgswarm/dawg-replay ~/dawg-replay
   git -C ~/dawg-replay sparse-checkout set skill
   ```

   Без git: на странице https://github.com/dawgswarm/dawg-replay нажмите Code, Download ZIP и скопируйте из архива папку `skill/skills/dawg-replay-builder`. На Windows каталог скиллов: `%USERPROFILE%\.agents\skills`. Для одного проекта вместо этого: `<проект>/.agents/skills/dawg-replay-builder`.

2. Подключите dawg-mcp в `~/.codex/config.toml`:

   ```toml
   [mcp_servers.dawg]
   command = "uvx"
   args = ["--with", "mcp<2", "dawg-mcp"]
   env_vars = ["DAWG_API_KEY"]
   ```

   `env_vars` передаёт серверу `DAWG_API_KEY` из окружения, в котором запущен Codex: перед запуском выполните `export DAWG_API_KEY=...` (или пропишите в профиль оболочки). Если dawg-mcp уже настроен (`codex mcp add dawg ...` или блок с `[mcp_servers.dawg.env]`), второй блок не добавляйте. `--with mcp<2` нужен, пока dawg-mcp не работает с `mcp` 2.x: без него сервер не стартует; в уже настроенном блоке добавьте его в `args`.

3. Перезапустите Codex. Проверка: `/skills` показывает `dawg-replay-builder`, `/mcp` показывает сервер `dawg`. Вызвать явно: `$dawg-replay-builder`.

Обновление: `git -C ~/dawg-replay pull` и снова команда `cp` из шага 1.

## Разработка

Плагин живёт в папке `skill/` репозитория `dawgswarm/dawg-replay`, маркетплейс `dawg-replay` описан в `.claude-plugin/marketplace.json` в корне репозитория. Проверка из рабочей копии, из корня репозитория:

```
claude plugin validate .
claude plugin validate ./skill
claude --plugin-dir ./skill
```

Любая правка в `skill/` поднимает `version` в `.claude-plugin/plugin.json`, иначе `/plugin marketplace update` не увидит изменений.

`skills/dawg-replay-builder/contract.md` это копия `docs/contract.md`; тест `test_skill.py` следит, чтобы они совпадали. После правки контракта скопируйте файл заново.

## Лицензия

MIT, текст в [LICENSE](LICENSE).
