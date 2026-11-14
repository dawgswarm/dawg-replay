"""Мягкая отмена запуска (docs/contract.md, раздел 7).

Приложение отменяет запуск файлом остановки и, кроме Windows, сигналом SIGTERM
группе процессов, а через 15 секунд убивает дерево процессов. По умолчанию Python на
SIGTERM умирает сразу: блоки `finally` и `atexit` не выполняются, отложенные
события пропадают, браузеры не закрываются.

Kit ставит обработчик, который превращает сигнал в `SystemExit`. Исключение
разворачивает стек как обычный выход: срабатывают `finally`, контекстные
менеджеры и `atexit`, kit дописывает отложенные события и закрывает файл.
`except Exception` в скрипте его не перехватывает.

Обработчик срабатывает один раз. Повторный сигнал (например, тот же SIGTERM,
пересланный `uv run` дочернему процессу) игнорируется, чтобы не прервать уже
идущую уборку.

Файл остановки. Приложение передаёт путь в `DAWG_SCRIPT_STOP_FILE` и при отмене
создаёт этот файл. На Windows это основной путь: CTRL_BREAK убивает `uv run`
вместе со скриптом, а `time.sleep` на нём не просыпается. Kit держит поток,
который раз в 0,2 секунды смотрит на файл и, когда тот появился, будит главный
поток сигналом: на Windows SIGINT (только он прерывает ожидание в `time.sleep`),
на POSIX SIGTERM главному потоку. Дальше всё как при обычном сигнале.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType

EXIT_CANCELLED = 143  # 128 + SIGTERM, одинаково на всех ОС
ENV_STOP_FILE = "DAWG_SCRIPT_STOP_FILE"
STOP_POLL_SECONDS = 0.2

_received: int | None = None
_watcher: threading.Thread | None = None


def _signals() -> list[signal.Signals]:
    names = ("SIGTERM", "SIGBREAK")  # SIGBREAK есть только на Windows
    return [getattr(signal, n) for n in names if hasattr(signal, n)]


def _handler(signum: int, frame: FrameType | None) -> None:
    global _received
    if _received is not None:
        return
    _received = signum
    raise SystemExit(EXIT_CANCELLED)


def install(stop_file: str | None = None) -> bool:
    """Ставит обработчик. Работает только из главного потока.

    Обработчик, который поставил сам скрипт, не трогается. Возвращает True,
    если обработчик kit стоит хотя бы на одном сигнале. `stop_file` по
    умолчанию берётся из `DAWG_SCRIPT_STOP_FILE`; если он есть, kit следит
    за файлом остановки.
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
    if stop_file is None:
        stop_file = os.environ.get(ENV_STOP_FILE) or None
    if stop_file and installed:
        if sys.platform == "win32" and signal.getsignal(signal.SIGINT) is signal.default_int_handler:
            # Под приложением Ctrl+C до скрипта не доходит (своя группа процессов), SIGINT нужен только для побудки.
            signal.signal(signal.SIGINT, _handler)
        watch(stop_file)
    return installed


def watch(stop_file: str, interval: float = STOP_POLL_SECONDS) -> None:
    """Запускает поток, который будит главный поток, когда появится `stop_file`."""
    global _watcher
    if _watcher is not None and _watcher.is_alive():
        return
    _watcher = threading.Thread(target=_watch, args=(stop_file, interval), name="dawg-replay-kit-stop", daemon=True)
    _watcher.start()


def _watch(stop_file: str, interval: float) -> None:
    while _received is None:
        if os.path.exists(stop_file):
            _wake_main()
            return
        threading.Event().wait(interval)


def _wake_main() -> None:
    if sys.platform == "win32":
        if signal.getsignal(signal.SIGINT) is _handler:
            # raise в этом потоке вызывает C-обработчик Python, он будит time.sleep главного потока.
            signal.raise_signal(signal.SIGINT)
        else:
            import _thread

            _thread.interrupt_main(signal.SIGBREAK)  # type: ignore[attr-defined]
    else:
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)  # type: ignore[arg-type]


def received() -> int | None:
    """Номер сигнала отмены, если он пришёл."""
    return _received
