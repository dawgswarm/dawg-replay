# dawg-replay

Локальное приложение для запуска скриптов сбора данных через облачные браузеры [DAWG](https://dawgswarm.ru). Скрипт один раз собирает кодинг-агент (Claude Code или Codex) со скиллом dawg-replay-builder, дальше вы запускаете его сами: в веб-интерфейсе меняете параметры, следите за прогрессом и скачиваете результат. Работает на macOS, Windows и Linux, нужен только [uv](https://docs.astral.sh/uv/).

```sh
uv tool install dawg-replay
dawg-replay config set-key     # ключ DAWG в системное хранилище паролей
dawg-replay ui                 # веб-интерфейс в браузере
```

Регулярные запуски из cron или Планировщика задач Windows:

```sh
dawg-replay run <скрипт> --preset <пресет> --follow
```

Полная инструкция от установки до первого скрипта: https://github.com/dawgswarm/dawg-replay#readme

Скрипты выполняются на вашем компьютере с вашими правами, песочницы нет. Добавляйте только скрипты, которым доверяете.

Лицензия MIT.
