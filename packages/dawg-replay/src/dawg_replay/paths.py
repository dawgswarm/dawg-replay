"""Каталог данных приложения (раздел 6.2 спеки)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "dawg-replay"
ENV_HOME = "DAWG_REPLAY_HOME"


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
    def dev_kit(self) -> Path:
        """Сборки локального kit для режима разработки."""
        return self.home / "dev-kit"

    def script_dir(self, name: str) -> Path:
        return self.scripts / name

    def script_file(self, name: str, version: int) -> Path:
        return self.scripts / name / f"v{version}" / "script.py"

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id

    def ensure(self) -> None:
        for d in (self.home, self.scripts, self.runs):
            d.mkdir(parents=True, exist_ok=True)
