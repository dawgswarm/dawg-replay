# dawg-replay-kit

Библиотека для скриптов приложения [dawg-replay](https://pypi.org/project/dawg-replay/): параметры запуска, события прогресса и результата, облачные браузеры DAWG, которые закрываются при выходе, ошибке и отмене.

Пакет нужен тем, кто пишет скрипты для dawg-replay. Обычно это кодинг-агент со скиллом dawg-replay-builder, руками ставить пакет не нужно: скрипт объявляет его в заголовке PEP 723, и `uv run` подтягивает его сам.

```python
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.1,<2"]
# ///
import dawg_replay_kit as kit

kit.stage("Сбор")
kit.progress("pages", "Страницы", 1, 5)
kit.result("ok", "собрано 120 карточек")
```

Мажорная версия kit совпадает с мажорной версией контракта скрипта. Контракт (манифест, типы параметров, события, окружение запуска): https://github.com/dawgswarm/dawg-replay/blob/main/docs/contract.md
