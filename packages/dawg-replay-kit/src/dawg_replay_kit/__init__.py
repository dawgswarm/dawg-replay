"""dawg-replay-kit: библиотека для скриптов dawg-replay.

    import dawg_replay_kit as kit

    kit.stage("Поиск")
    for page in range(1, kit.params.max_pages + 1):
        ...
        kit.progress("pages", "Страницы", page, kit.params.max_pages)
    kit.result("ok", "Собрано 120 карточек")

Окружение читается при первом обращении к kit: манифест из файла скрипта,
параметры из `DAWG_SCRIPT_PARAMS` или из аргументов командной строки.
Если параметры не подходят, скрипт завершается с кодом 2 и объяснением в stderr.

При импорте kit переводит stdout и stderr в UTF-8: на Windows вывод в файл
иначе идёт в кодировке локали, и кириллица в print ломает скрипт.

При инициализации kit ставит обработчик SIGTERM (на Windows ещё CTRL_BREAK)
и следит за файлом остановки из `DAWG_SCRIPT_STOP_FILE`: отмена запуска из
приложения превращается в SystemExit(143), поэтому `finally` и `atexit`
срабатывают, а отложенные события дописываются в файл.
"""

from __future__ import annotations

import atexit
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dawg_replay_kit import cancel, stdio
from dawg_replay_kit.ask import AskTimeout
from dawg_replay_kit.browser import BrowserLimitReached, BrowserSession
from dawg_replay_kit.dawg_api import BrowserError, BrowserLimitError
from dawg_replay_kit.events import Log
from dawg_replay_kit.logins import LoginError
from dawg_replay_kit.manifest import SUPPORTED_CONTRACTS
from dawg_replay_kit.runtime import EXIT_BAD_PARAMS, Kit, StartupError

__version__ = "1.4.0"  # мажор равен мажору контракта; версия задаётся только здесь

__all__ = [
    "SUPPORTED_CONTRACTS",
    "AskTimeout",
    "BrowserError",
    "BrowserLimitError",
    "BrowserLimitReached",
    "BrowserSession",
    "Kit",
    "LoginError",
    "StartupError",
    "__version__",
    "artifact",
    "ask",
    "browser",
    "init",
    "item",
    "log",
    "metric",
    "progress",
    "result",
    "rows",
    "set_browser_limit",
    "stage",
]

stdio.use_utf8()

_kit: Kit | None = None
_lock = threading.Lock()


def init(**kwargs: Any) -> Kit:
    """Явная инициализация. Обычно не нужна: kit поднимается при первом обращении.

    Аргументы те же, что у `Kit.from_environment`. Ошибку старта kit печатает
    в stderr и завершает процесс с кодом 2.
    """
    global _kit
    with _lock:
        if _kit is None:
            try:
                _kit = Kit.from_environment(**kwargs)
            except StartupError as e:
                print(f"dawg-replay-kit: {e}", file=sys.stderr, flush=True)
                raise SystemExit(EXIT_BAD_PARAMS) from None
            atexit.register(_kit.close)
            # Сигнал отмены и файл остановки превращаются в SystemExit: finally и atexit успевают отработать.
            cancel.install()
        return _kit


def _runtime() -> Kit:
    return _kit if _kit is not None else init()


def __getattr__(name: str) -> Any:
    if name in ("params", "output_dir", "run_id", "autonomous"):
        return getattr(_runtime(), name)
    raise AttributeError(f"module 'dawg_replay_kit' has no attribute {name!r}")


log = Log(lambda: _runtime().events)


def stage(name: str) -> None:
    _runtime().events.stage(name)


def progress(id: str, label: str, done: int | float, total: int | float | None = None) -> None:
    _runtime().events.progress(id, label, done, total)


def metric(key: str, label: str, value: int | float | str, unit: str | None = None) -> None:
    _runtime().events.metric(key, label, value, unit)


def rows(table: str, rows: Any, columns: Any = None) -> None:
    _runtime().events.rows(table, rows, columns)


def item(key: str | int, status: str, msg: Any = None, row: int | None = None) -> None:
    """Итог по одной строке входа (`ok`, `failed`, `skipped`). Повтор с тем же key заменяет прежний.
    row: поле `_row` строки таблицы, по нему приложение перезапускает упавшие строки."""
    _runtime().events.item(key, status, msg, row)


def artifact(path: str | Path, label: str, mime: str | None = None) -> None:
    _runtime().events.artifact(path, label, mime)


def result(
    status: str,
    summary: str,
    count: int | None = None,
    links: Sequence[Mapping[str, Any]] | None = None,
    main: str | Path | None = None,
) -> None:
    """Итог запуска: `ok`, `partial` или `failed` и короткий текст.

    Необязательно: `count` сколько записей или строк собрано (по нему приложение замечает
    тихую поломку), `links` список {"title": ..., "url": ...}, `main` главный файл результата
    в kit.output_dir: на экране запуска кнопка «Открыть результат» первой.
    """
    _runtime().events.result(status, summary, count, links, main)


def ask(
    question: str,
    kind: str = "text",
    choices: Sequence[str] | None = None,
    secret: bool = False,
    timeout_minutes: float = 30,
) -> str | bool:
    """Спросить человека посреди запуска и дождаться ответа.

    kind: `text` (строка), `confirm` (True или False), `choice` (одна строка из `choices`).
    `secret=True` для кодов и паролей: ответ не попадает в события и логи.
    Нет ответа за `timeout_minutes`: `AskTimeout`. Время ожидания входит в общий
    лимит времени запуска, минуты браузеров DAWG при этом тоже идут.
    В автономном режиме вопрос задаётся в терминале.
    """
    return _runtime().asker.ask(question, kind, choices, secret, timeout_minutes)


def browser(
    proxy: str | None = None,
    geo: str | tuple[float, float] | None = None,
    ready_timeout: float = 120.0,
    login: str | None = None,
) -> BrowserSession:
    """Браузер DAWG для `with` или `async with`.

    proxy: `socks5://user:pass@host:port` и подобное; geo: slug города (`moskva`)
    или координаты `"55.75,37.61"`. Внутри доступны `.page`, `.context`, `.browser`,
    `.session_id`.

    login: имя сохранённого входа (`row["email"]`, `"кабинет"`). Kit загружает его cookies
    и localStorage перед работой и сохраняет при закрытии; `.login_restored` говорит,
    было ли что загружать, `.forget_login()` стирает вход. Залогинен ли браузер,
    скрипт проверяет сам.
    """
    return BrowserSession(_runtime().browsers, proxy, geo, ready_timeout, login=login)


def set_browser_limit(n: int) -> None:
    """Сколько браузеров скрипт держит одновременно. По умолчанию 1."""
    _runtime().browsers.set_limit(n)
