"""Реестр скриптов: регистрация версий, текущая версия, откат, удаление."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dawg_replay_kit.manifest import Manifest, ManifestError, load_manifest, parse_manifest

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.paths import Paths


@dataclass(frozen=True)
class Version:
    name: str
    version: int
    title: str
    description: str | None
    contract: int
    sha256: str
    path: Path
    added_at: str
    current: bool
    site: str | None = None
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "site": self.site,
            "tags": list(self.tags),
            "contract": self.contract,
            "sha256": self.sha256,
            "path": str(self.path),
            "added_at": self.added_at,
            "current": self.current,
        }


@dataclass(frozen=True)
class Script:
    name: str
    current_version: int
    title: str
    description: str | None
    versions_count: int
    created_at: str
    updated_at: str
    site: str | None = None  # из манифеста текущей версии (контракт 1.1)
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current_version": self.current_version,
            "title": self.title,
            "description": self.description,
            "site": self.site,
            "tags": list(self.tags),
            "versions_count": self.versions_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AddResult:
    status: str  # "added" или "unchanged"
    script: Script
    version: Version
    previous_version: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "script": self.script.to_dict(),
            "version": self.version.to_dict(),
            "previous_version": self.previous_version,
        }


def invalid_manifest(e: ManifestError, file: str | Path) -> ReplayError:
    return ReplayError(
        "invalid_manifest",
        f"{file}: манифест не прошёл проверку, ошибок: {len(e.issues)}\n" + "\n".join(f"  {i}" for i in e.issues),
        [i.to_dict() for i in e.issues],
    )


def _site_tags(manifest_json: str) -> dict[str, Any]:
    """site и tags из сохранённого манифеста; у версий до контракта 1.1 их нет."""
    try:
        m = json.loads(manifest_json)
    except (TypeError, ValueError):
        return {}
    return {"site": m.get("site"), "tags": tuple(m.get("tags") or ())}


class Registry:
    def __init__(self, conn: sqlite3.Connection, paths: Paths):
        self._db = conn
        self._paths = paths

    # --- чтение ---

    def _script_row(self, name: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM scripts WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ReplayError("script_not_found", f"скрипт `{name}` не зарегистрирован; список: dawg-replay list")
        return row

    def get(self, name: str) -> Script:
        row = self._script_row(name)
        return self._script(row)

    def _script(self, row: sqlite3.Row) -> Script:
        v = self._db.execute(
            "SELECT title, description, manifest_json FROM script_versions WHERE name = ? AND version = ?",
            (row["name"], row["current_version"]),
        ).fetchone()
        count = self._db.execute("SELECT COUNT(*) FROM script_versions WHERE name = ?", (row["name"],)).fetchone()[0]
        return Script(
            name=row["name"],
            current_version=row["current_version"],
            title=v["title"],
            description=v["description"],
            versions_count=count,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            **_site_tags(v["manifest_json"]),
        )

    def list(self) -> list[Script]:
        rows = self._db.execute("SELECT * FROM scripts ORDER BY name").fetchall()
        return [self._script(r) for r in rows]

    def versions(self, name: str) -> list[Version]:
        current = self._script_row(name)["current_version"]
        rows = self._db.execute(
            "SELECT * FROM script_versions WHERE name = ? ORDER BY version", (name,)
        ).fetchall()
        return [self._version(r, current, self._paths.home) for r in rows]

    def version(self, name: str, version: int | None = None) -> Version:
        current = self._script_row(name)["current_version"]
        v = current if version is None else version
        row = self._db.execute(
            "SELECT * FROM script_versions WHERE name = ? AND version = ?", (name, v)
        ).fetchone()
        if row is None:
            have = ", ".join(str(x.version) for x in self.versions(name))
            raise ReplayError("version_not_found", f"у скрипта `{name}` нет версии {v}; есть: {have}")
        return self._version(row, current, self._paths.home)

    @staticmethod
    def _version(row: sqlite3.Row, current: int, home: Path) -> Version:
        return Version(
            name=row["name"],
            version=row["version"],
            title=row["title"],
            description=row["description"],
            contract=row["contract"],
            sha256=row["sha256"],
            path=home / row["path"],  # путь хранится относительно каталога данных
            added_at=row["added_at"],
            current=row["version"] == current,
            **_site_tags(row["manifest_json"]),
        )

    def manifest(self, name: str, version: int | None = None) -> Manifest:
        v = self.version(name, version)
        try:
            return load_manifest(v.path)
        except ManifestError as e:
            raise ReplayError(
                "stored_script_broken",
                f"файл версии {v.version} скрипта `{name}` повреждён или удалён: {v.path}\n{e}",
            ) from None

    # --- изменение ---

    def add(self, file: str | Path) -> AddResult:
        src = Path(file)
        try:
            data = src.read_bytes()
        except FileNotFoundError:
            raise ReplayError("file_not_found", f"файл не найден: {src}") from None
        except OSError as e:
            raise ReplayError("file_unreadable", f"не удалось прочитать {src}: {e.strerror}") from None
        try:
            manifest = parse_manifest(data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            raise ReplayError("bad_encoding", f"файл не в UTF-8: {src}") from None
        except ManifestError as e:
            raise invalid_manifest(e, src) from None

        name, version = manifest.name, manifest.version
        sha = hashlib.sha256(data).hexdigest()
        existing = self._db.execute("SELECT * FROM scripts WHERE name = ?", (name,)).fetchone()
        previous = existing["current_version"] if existing else None

        if existing:
            same = self._db.execute(
                "SELECT sha256 FROM script_versions WHERE name = ? AND version = ?", (name, version)
            ).fetchone()
            if same is not None:
                if same["sha256"] == sha:
                    return AddResult("unchanged", self.get(name), self.version(name, version), previous)
                raise ReplayError(
                    "version_exists",
                    f"версия {version} скрипта `{name}` уже зарегистрирована с другим содержимым. "
                    "Поднимите `version` в манифесте",
                )
            top = self._db.execute("SELECT MAX(version) FROM script_versions WHERE name = ?", (name,)).fetchone()[0]
            if version < top:
                raise ReplayError(
                    "version_too_low",
                    f"у скрипта `{name}` уже есть версия {top}, новая должна быть больше. "
                    f"Поставьте `version = {top + 1}`",
                )

        dest = self._paths.script_file(name, version)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".py.tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)

        now = utcnow()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            if existing:
                self._db.execute(
                    "UPDATE scripts SET current_version = ?, updated_at = ? WHERE name = ?", (version, now, name)
                )
            else:
                self._db.execute(
                    "INSERT INTO scripts (name, current_version, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (name, version, now, now),
                )
            self._db.execute(
                "INSERT INTO script_versions (name, version, title, description, contract, manifest_json, sha256, path, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    version,
                    manifest.title,
                    manifest.description,
                    manifest.contract,
                    json.dumps(manifest.to_dict(), ensure_ascii=False),
                    sha,
                    dest.relative_to(self._paths.home).as_posix(),
                    now,
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            dest.unlink(missing_ok=True)
            raise
        return AddResult("added", self.get(name), self.version(name, version), previous)

    def rollback(self, name: str, version: int) -> Version:
        self.version(name, version)  # проверка, что версия есть
        self._db.execute(
            "UPDATE scripts SET current_version = ?, updated_at = ? WHERE name = ?", (version, utcnow(), name)
        )
        return self.version(name)

    def remove(self, name: str) -> list[Version]:
        """Удаляет скрипт, все версии и пресеты. История запусков остаётся."""
        versions = self.versions(name)
        self._db.execute("DELETE FROM scripts WHERE name = ?", (name,))
        shutil.rmtree(self._paths.script_dir(name), ignore_errors=True)
        return versions
