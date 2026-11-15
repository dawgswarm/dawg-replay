"""Каталог данных приложения (раздел 6.2 спеки)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "dawg-replay"
ENV_HOME = "DAWG_REPLAY_HOME"


_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _part(value: str) -> str:
    """Имя скрипта или id запуска как один компонент пути: без `..`, разделителей и управляющих символов.

    Имена и id проверяются раньше (манифест, БД); это последняя защита перед rmtree и записью файлов.
    """
    if not isinstance(value, str) or not _PART_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"недопустимое имя в пути: {value!r}")
    return value


def default_home() -> Path:
    """`DAWG_REPLAY_HOME`, если задан, иначе каталог данных ОС через platformdirs."""
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    return platformdirs.user_data_path(APP_NAME, appauthor=False)


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def db(self) -> Path:
        return self.home / "db.sqlite"

    @property
    def scripts(self) -> Path:
        return self.home / "scripts"

    @property
    def runs(self) -> Path:
        return self.home / "runs"

    @property
    def secrets_tmp(self) -> Path:
        """Временные файлы параметров с секретами живых запусков (0700)."""
        return self.home / "tmp"

    @property
    def imports(self) -> Path:
        """Файлы, загруженные через интерфейс и ещё не добавленные (импорт скрипта)."""
        return self.home / "imports"

    @property
    def uploads(self) -> Path:
        """Таблицы параметров `table`, загруженные через форму (0700, сутки)."""
        return self.home / "uploads"

    @property
    def schedules(self) -> Path:
        """Логи регулярных запусков и обёртки задач Планировщика Windows."""
        return self.home / "schedules"

    @property
    def dev_kit(self) -> Path:
        """Сборки локального kit для режима разработки."""
        return self.home / "dev-kit"

    def script_dir(self, name: str) -> Path:
        return self.scripts / _part(name)

    def script_file(self, name: str, version: int) -> Path:
        return self.scripts / _part(name) / f"v{int(version)}" / "script.py"

    def run_dir(self, run_id: str) -> Path:
        return self.runs / _part(run_id)

    def ensure(self) -> None:
        for d in (self.home, self.scripts, self.runs):
            d.mkdir(parents=True, exist_ok=True)
