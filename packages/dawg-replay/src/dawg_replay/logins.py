"""Сохранённые входы скриптов на стороне приложения (ТЗ M3, раздел 4).

Входы пишет kit (`kit.browser(login=...)`) в `<home>/logins/<script>/`, по файлу
на вход; формат и хеш имени файла задаёт `dawg_replay_kit.logins`. Приложение
только показывает список (имя, когда сохранён и использован) и стирает входы.
Содержимое входа (cookies, localStorage) наружу не отдаётся: ни в UI, ни в CLI,
ни в историю запусков.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from dawg_replay_kit.logins import LoginError, LoginStore

from dawg_replay.errors import ReplayError
from dawg_replay.paths import Paths

_IS_WINDOWS = sys.platform == "win32"


def prepare_dir(paths: Paths, script: str) -> Path:
    """Каталог входов скрипта для `DAWG_SCRIPT_LOGINS_DIR`. Общий `<home>/logins` создаётся
    сразу 0700: mkdir(parents=True) дал бы промежуточным каталогам права по umask.
    Сам каталог скрипта kit создаёт при первом сохранении."""
    for d in (paths.logins, paths.login_dir(script)):
        d.mkdir(exist_ok=True, mode=0o700)
        if not _IS_WINDOWS:
            os.chmod(d, 0o700)
    return paths.login_dir(script)


def _store(paths: Paths, script: str) -> LoginStore:
    return LoginStore(paths.login_dir(script))


def list_logins(paths: Paths, script: str) -> list[dict[str, Any]]:
    """Имя, когда сохранён, когда использован. Без cookies и localStorage."""
    return [login.info() for login in _store(paths, script).list()]


def forget(paths: Paths, script: str, name: str) -> None:
    try:
        removed = _store(paths, script).forget(name)
    except LoginError as e:
        raise ReplayError("bad_login_name", str(e)) from None
    if not removed:
        raise ReplayError("login_not_found", f"у скрипта `{script}` нет сохранённого входа «{name}»")


def forget_all(paths: Paths, script: str) -> int:
    return _store(paths, script).forget_all()


def remove_dir(paths: Paths, script: str) -> int:
    """Удаление скрипта: каталог входов целиком. Возвращает, сколько входов было."""
    d = paths.login_dir(script)
    if not d.exists():
        return 0
    n = len(_store(paths, script).list())
    shutil.rmtree(d, ignore_errors=True)
    return n
