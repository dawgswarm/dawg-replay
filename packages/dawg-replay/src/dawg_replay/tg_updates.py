"""Единственный получатель обновлений Telegram (`getUpdates`) на установку.

Telegram отвечает конфликтом, если `getUpdates` зовут параллельно, а ждать ответа
могут сразу несколько запусков (каждый в своём процессе) и привязка чата. Поэтому
`getUpdates` зовёт только тот, кто держит файл-блокировку `<home>/telegram/updates.lock`
(`updates_lock`). Блокировку держит процесс, ОС снимает её, если он упал, и её
подхватывает другой ждущий процесс.

Подтверждённое место в потоке обновлений (`offset`) лежит в `<home>/telegram/offset`
и общее для всех: тот, кто взял блокировку, продолжает с него и сохраняет его
(`save_offset`) после обработки каждого обновления. Обновление, полученное, но не
обработанное до падения, Telegram отдаст следующему опрашивающему.

Правила для держателя:
- опрос ответов на вопросы (`dawg_replay.tg_asks`) держит блокировку, только пока
  его запуск ждёт ответа из Telegram, иначе привязка чата ждала бы вечно;
- привязка чата (`telegram connect`, Настройки) берёт блокировку с ожиданием на время
  ожидания `/start <код>` и тоже сохраняет `offset` через `save_offset`. Если в это
  время придёт ответ на вопрос скрипта, привязка его не обрабатывает и он теряется
  (вопрос остаётся ждать, человек может ответить ещё раз). Это редкий случай:
  привязка нового чата во время ждущего вопроса старого чата.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

LOCK_FILE = "updates.lock"
OFFSET_FILE = "offset"
_RETRY = 0.2


def telegram_dir(home: Path) -> Path:
    d = Path(home) / "telegram"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _home(app_or_home: Any) -> Path:
    paths = getattr(app_or_home, "paths", None)
    return Path(paths.home) if paths is not None else Path(app_or_home)


if sys.platform == "win32":  # pragma: no cover - проверяется на Windows
    import msvcrt

    def _try_lock(f: BinaryIO) -> bool:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(f: BinaryIO) -> None:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _try_lock(f: BinaryIO) -> bool:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(f: BinaryIO) -> None:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class UpdatesLock:
    """Блокировка получателя обновлений. Не реентерабельна; flock действует и между
    потоками одного процесса, потому что каждый `acquire` открывает файл заново."""

    def __init__(self, app_or_home: Any):
        self.path = telegram_dir(_home(app_or_home)) / LOCK_FILE
        self._f: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._f is not None

    def acquire(self, wait: float | None = 0.0) -> bool:
        """Берёт блокировку. `wait`: сколько секунд ждать (0 сразу, None без предела)."""
        if self._f is not None:
            return True
        deadline = None if wait is None else time.monotonic() + wait
        while True:
            f = open(self.path, "a+b")
            if _try_lock(f):
                self._f = f
                return True
            f.close()
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(_RETRY if deadline is None else max(0.0, min(_RETRY, deadline - time.monotonic())))

    def release(self) -> None:
        f, self._f = self._f, None
        if f is not None:
            _unlock(f)
            f.close()


@contextmanager
def updates_lock(app_or_home: Any, wait: float | None = 0.0) -> Iterator[bool]:
    """`with updates_lock(app, wait=…) as got:` got=True, если блокировка взята."""
    lock = UpdatesLock(app_or_home)
    got = lock.acquire(wait)
    try:
        yield got
    finally:
        if got:
            lock.release()


def load_offset(app_or_home: Any) -> int | None:
    try:
        text = (telegram_dir(_home(app_or_home)) / OFFSET_FILE).read_text(encoding="ascii").strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def save_offset(app_or_home: Any, offset: int) -> None:
    """Следующий `offset` для `getUpdates`: update_id обработанного обновления + 1."""
    d = telegram_dir(_home(app_or_home))
    tmp = d / f".{OFFSET_FILE}.{os.getpid()}.tmp"
    tmp.write_text(str(int(offset)), encoding="ascii")
    os.replace(tmp, d / OFFSET_FILE)
