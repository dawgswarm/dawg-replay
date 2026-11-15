"""Запуски, начатые из веб-интерфейса (ТЗ 6.6).

Запуск живёт в процессе сервера UI: на каждый запуск поток, в котором
`launch.supervise` ведёт процесс скрипта, как при `run --follow`. Супервизором
в БД записан процесс сервера.

- Остановка сервера (Ctrl+C, SIGTERM): `cancel_all` отменяет все его запуски по 5.7
  (мягкий сигнал, через grace kill дерева) и ждёт итога со страховкой браузеров 5.6.
- Сервер убит жёстко: супервизора больше нет, запуск закрывает `recover_lost` при
  следующем старте приложения или любой команде CLI: дерево скрипта убивается,
  страховка освобождает браузеры.
- `dawg-replay cancel <id>` из CLI работает и для этих запусков: supervise видит
  запрос отмены в БД.

Отдельный процесс-супервизор (как у фонового `run`) здесь не нужен: спека требует,
чтобы при закрытии сервера его запуски отменялись.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.runs import Run

# Сколько ждать запуски при остановке сверх grace: страховка, запись итога.
SHUTDOWN_MARGIN = 30.0


class UiRuns:
    def __init__(self, home: Path, app_factory: Callable[[], App] | None = None):
        self._home = home
        self._app_factory = app_factory or (lambda: App(home))
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()  # общий сигнал отмены для всех запусков сервера
        self._closed = False

    def start(
        self,
        target: str,
        *,
        preset: str | None = None,
        sets: Sequence[str] | None = None,
        values: Mapping[str, Any] | None = None,
        strict_preset: bool = False,
        notify: str | None = None,
    ) -> Run:
        """Создаёт запуск и ведёт его в отдельном потоке. Ошибки подготовки (параметры,
        лимит параллельных запусков) бросаются сразу как ReplayError."""
        if self._closed:
            from dawg_replay.errors import ReplayError

            raise ReplayError("server_stopping", "сервер останавливается, новые запуски не принимаются")
        with self._app_factory() as app:
            prepared = launch.prepare(app, target, preset=preset, sets=sets, values=values, strict_preset=strict_preset,
                                      notify=notify)
            run = prepared.run
        payload = prepared.payload()
        t = threading.Thread(target=self._supervise, args=(run.id, payload), name=f"dawg-ui-run-{run.id}", daemon=True)
        with self._lock:
            closed = self._closed
            if not closed:
                self._threads[run.id] = t
        if closed:
            # Сервер начал останавливаться, пока готовился запуск: процесс не стартует.
            with self._app_factory() as app:
                app.runs.finish(run.id, status="failed", reason="cancelled", error="сервер UI остановлен до старта")
                return app.runs.get(run.id)
        t.start()
        return run

    def _supervise(self, run_id: str, payload: dict) -> None:
        try:
            # Своё подключение к БД в своём потоке.
            with self._app_factory() as app:
                launch.supervise(app, run_id, payload, interrupted=self._stop)
        finally:
            with self._lock:
                self._threads.pop(run_id, None)

    def active(self) -> list[str]:
        with self._lock:
            return list(self._threads)

    def cancel_all(self, timeout: float | None = None) -> list[str]:
        """Отменяет все запуски сервера и ждёт их итога. Возвращает id тех, что не успели завершиться."""
        with self._lock:
            self._closed = True
            threads = list(self._threads.items())
        self._stop.set()
        if timeout is None:
            with self._app_factory() as app:
                timeout = app.config.cancel_grace_seconds + SHUTDOWN_MARGIN
        import time

        deadline = time.monotonic() + timeout
        for _, t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return [rid for rid, t in threads if t.is_alive()]
