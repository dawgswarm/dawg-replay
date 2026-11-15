"""Секретные таблицы пресетов (параметр `table` с `secret = true`).

Таблица лежит отдельным файлом `<home>/preset-tables/<скрипт>/<ref>.json` (каталог 0700,
файл 0600, запись через временный файл). В БД у пресета только ссылка `ref`, имя исходного
файла и число строк, поэтому таблица не попадает ни в БД, ни в историю запусков, ни в логи.
Запуск из пресета передаёт её скрипту как секрет, как загруженную вручную.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

from dawg_replay.paths import Paths

_REF_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")


class PresetTables:
    def __init__(self, paths: Paths):
        self.paths = paths

    def put(self, script: str, rows: list[dict[str, Any]]) -> str:
        d = self._dir(script)
        for p in (self.paths.preset_tables, d):
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, 0o700)
        ref = secrets.token_urlsafe(16)
        path = d / f"{ref}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        tmp.replace(path)
        return ref

    def put_file(self, script: str, source: str | Path) -> str:
        """Копия файла параметра `file` (dr-84) для пресета: `<ref>.bin`, 0600."""
        d = self._dir(script)
        for p in (self.paths.preset_tables, d):
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, 0o700)
        ref = secrets.token_urlsafe(16)
        path = d / f"{ref}.bin"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as dst, open(source, "rb") as src:
            shutil.copyfileobj(src, dst)
        tmp.replace(path)
        return ref

    def file_path(self, script: str, ref: Any) -> Path | None:
        if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
            return None
        path = self._dir(script) / f"{ref}.bin"
        return path if path.is_file() else None

    def get(self, script: str, ref: Any) -> list[dict[str, Any]] | None:
        if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
            return None
        try:
            rows = json.loads((self._dir(script) / f"{ref}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return rows if isinstance(rows, list) else None

    def delete(self, script: str, ref: Any) -> None:
        if isinstance(ref, str) and _REF_RE.fullmatch(ref):
            (self._dir(script) / f"{ref}.json").unlink(missing_ok=True)
            (self._dir(script) / f"{ref}.bin").unlink(missing_ok=True)

    def remove_script(self, script: str) -> None:
        shutil.rmtree(self._dir(script), ignore_errors=True)

    def _dir(self, script: str) -> Path:
        return self.paths.preset_tables_dir(script)
