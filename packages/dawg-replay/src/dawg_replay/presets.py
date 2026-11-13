"""Пресеты: именованные наборы значений параметров. Секреты в пресеты не попадают."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from dawg_replay_kit.manifest import Manifest

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.values import check_partial, filter_known

_NAME_RE = re.compile(r"^[\w.-]{1,64}$")


@dataclass(frozen=True)
class Preset:
    script: str
    name: str
    params: dict[str, Any]  # только то, что подходит текущей версии
    dropped: list[str]  # ключи, которые текущая версия не принимает
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "script": self.script,
            "name": self.name,
            "params": self.params,
            "dropped": self.dropped,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Presets:
    """Значения хранятся как сохранены. При чтении они фильтруются по манифесту
    текущей версии, поэтому откат версии не теряет данные пресета."""

    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    def save(self, script: str, name: str, manifest: Manifest, values: dict[str, Any]) -> Preset:
        if not _NAME_RE.match(name):
            raise ReplayError("bad_preset_name", f"имя пресета {name!r}: буквы, цифры, `_`, `-`, `.`, до 64 символов")
        problems = check_partial(manifest, values, allow_secrets=False)
        if problems:
            raise ReplayError(
                "bad_params", "значения не подходят:\n" + "\n".join(f"  {x}" for x in problems), problems
            )
        now = utcnow()
        self._db.execute(
            "INSERT INTO presets (script_name, name, params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (script_name, name) DO UPDATE SET params_json = excluded.params_json, updated_at = excluded.updated_at",
            (script, name, json.dumps(values, ensure_ascii=False), now, now),
        )
        return self.get(script, name, manifest)

    def get(self, script: str, name: str, manifest: Manifest) -> Preset:
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
        cur = self._db.execute("DELETE FROM presets WHERE script_name = ? AND name = ?", (script, name))
        if cur.rowcount == 0:
            raise ReplayError("preset_not_found", f"у скрипта `{script}` нет пресета `{name}`")

    @staticmethod
    def _preset(row: sqlite3.Row, manifest: Manifest) -> Preset:
        kept, dropped = filter_known(manifest, json.loads(row["params_json"]))
        return Preset(row["script_name"], row["name"], kept, dropped, row["created_at"], row["updated_at"])
