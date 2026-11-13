"""Мягкая отмена запуска (docs/contract.md, раздел 7).

Приложение отменяет запуск сигналом SIGTERM (на Windows CTRL_BREAK_EVENT группе
процессов) и через 15 секунд убивает дерево процессов. По умолчанию Python на
SIGTERM умирает сразу: блоки `finally` и `atexit` не выполняются, отложенные
события пропадают, браузеры не закрываются.

Kit ставит обработчик, который превращает сигнал в `SystemExit`. Исключение
разворачивает стек как обычный выход: срабатывают `finally`, контекстные
менеджеры и `atexit`, kit дописывает отложенные события и закрывает файл.
`except Exception` в скрипте его не перехватывает.

Обработчик срабатывает один раз. Повторный сигнал (например, тот же SIGTERM,
пересланный `uv run` дочернему процессу) игнорируется, чтобы не прервать уже
идущую уборку.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

EXIT_CANCELLED = 143  # 128 + SIGTERM, одинаково на всех ОС

_received: int | None = None


def _signals() -> list[signal.Signals]:
    names = ("SIGTERM", "SIGBREAK")  # SIGBREAK есть только на Windows
    return [getattr(signal, n) for n in names if hasattr(signal, n)]


def _handler(signum: int, frame: FrameType | None) -> None:
    global _received
    if _received is not None:
        return
    _received = signum
    raise SystemExit(EXIT_CANCELLED)


def install() -> bool:
    """Ставит обработчик. Работает только из главного потока.

    Обработчик, который поставил сам скрипт, не трогается. Возвращает True,
    если обработчик kit стоит хотя бы на одном сигнале.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    installed = False
    for sig in _signals():
        current = signal.getsignal(sig)
        if current is _handler:
            installed = True
        elif current in (signal.SIG_DFL, None):
            signal.signal(sig, _handler)
            installed = True
    return installed


def received() -> int | None:
    """Номер сигнала отмены, если он пришёл."""
    return _received
