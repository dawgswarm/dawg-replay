"""Пресеты: именованные наборы значений параметров. Секреты в пресеты не попадают."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from dawg_replay_kit.manifest import Manifest

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.values import check_partial, filter_known

NAME_MAX = 64
# Имена файлов таблиц хранятся рядом со значениями. Имя параметра не начинается с `_`,
# поэтому ключ ни с чем не совпадёт.
FILES_KEY = "_files"


def normalize(name: str | None) -> str:
    """Пробелы по краям убираются, неразрывный пробел из копипасты становится обычным."""
    return (name or "").replace("\u00a0", " ").replace("\u202f", " ").strip()


def clean_name(name: str) -> str:
    """Имя пресета без пробелов по краям. Любые печатные символы, включая пробелы и
    кириллицу. Нельзя: пустое, длиннее 64, управляющие символы, `-` в начале
    (в `run --preset -x` такое имя примут за флаг)."""
    name = normalize(name)
    if not name:
        raise ReplayError("bad_preset_name", "имя пресета пустое")
    if len(name) > NAME_MAX:
        raise ReplayError("bad_preset_name", f"имя пресета длиннее {NAME_MAX} символов")
    if not name.isprintable():
        raise ReplayError("bad_preset_name", "в имени пресета есть перевод строки, табуляция или другой непечатный символ")
    if name.startswith("-"):
        raise ReplayError("bad_preset_name", "имя пресета не может начинаться с «-»")
    return name


@dataclass(frozen=True)
class Preset:
    script: str
    name: str
    params: dict[str, Any]  # только то, что подходит текущей версии
    dropped: list[str]  # ключи, которые текущая версия не принимает
    created_at: str
    updated_at: str
    files: dict[str, str] = field(default_factory=dict)  # параметр table -> имя исходного файла

    def to_dict(self) -> dict[str, Any]:
        return {
            "script": self.script,
            "name": self.name,
            "params": self.params,
            "dropped": self.dropped,
            "files": self.files,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Presets:
    """Значения хранятся как сохранены. При чтении они фильтруются по манифесту
    текущей версии, поэтому откат версии не теряет данные пресета."""

    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    def save(
        self, script: str, name: str, manifest: Manifest, values: dict[str, Any], files: dict[str, str] | None = None
    ) -> Preset:
        """`files`: имена файлов таблиц. Таблица с `secret = true` в пресет не попадает."""
        name = clean_name(name)
        secret_tables = {p.name for p in manifest.params if p.type == "table" and p.secret}
        values = {k: v for k, v in values.items() if k not in secret_tables}
        files = {k: v for k, v in (files or {}).items() if k in values}
        problems = check_partial(manifest, values, allow_secrets=False)
        if problems:
            raise ReplayError(
                "bad_params", "значения не подходят:\n" + "\n".join(f"  {x}" for x in problems), problems
            )
        now = utcnow()
        self._db.execute(
            "INSERT INTO presets (script_name, name, params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (script_name, name) DO UPDATE SET params_json = excluded.params_json, updated_at = excluded.updated_at",
            (script, name, json.dumps({**values, FILES_KEY: files} if files else values, ensure_ascii=False), now, now),
        )
        return self.get(script, name, manifest)

    def get(self, script: str, name: str, manifest: Manifest) -> Preset:
        name = normalize(name)
        row = self._db.execute(
            "SELECT * FROM presets WHERE script_name = ? AND name = ?", (script, name)
        ).fetchone()
        if row is None:
            raise ReplayError("preset_not_found", f"у скрипта `{script}` нет пресета `{name}`")
        return self._preset(row, manifest)

    def list(self, script: str, manifest: Manifest) -> list[Preset]:
        rows = self._db.execute("SELECT * FROM presets WHERE script_name = ? ORDER BY name", (script,)).fetchall()
        return [self._preset(r, manifest) for r in rows]

    def delete(self, script: str, name: str) -> None:
        name = normalize(name)
        cur = self._db.execute("DELETE FROM presets WHERE script_name = ? AND name = ?", (script, name))
        if cur.rowcount == 0:
            raise ReplayError("preset_not_found", f"у скрипта `{script}` нет пресета `{name}`")

    @staticmethod
    def _preset(row: sqlite3.Row, manifest: Manifest) -> Preset:
        stored = json.loads(row["params_json"])
        files = stored.pop(FILES_KEY, None)
        kept, dropped = filter_known(manifest, stored)
        files = {k: v for k, v in files.items() if k in kept and isinstance(v, str)} if isinstance(files, dict) else {}
        return Preset(row["script_name"], row["name"], kept, dropped, row["created_at"], row["updated_at"], files)
