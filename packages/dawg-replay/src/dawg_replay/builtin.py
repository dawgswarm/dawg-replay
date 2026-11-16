"""Стандартная библиотека узлов: обычные скрипты по контракту, которые поставляются с приложением.

    sync(app)        # при старте App: поставить или обновить, если библиотека в пакете поменялась
    sync(app, True)  # то же без сверки отпечатка

Файлы лежат в пакете (`dawg_replay/stdlib/*.py`). Отпечаток содержимого хранится в `app_meta`:
пока он совпадает, старт ничего не делает; после установки или обновления приложения узлы
регистрируются заново (новые версии добавляются, как `dawg-replay add`). Скрипты помечаются
`builtin`; скрипт человека с тем же `name` не трогается, а человек не может положить свою
версию поверх встроенного узла. Удалённый человеком узел вернётся со следующим обновлением.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay.errors import ReplayError

if TYPE_CHECKING:
    from dawg_replay.app import App

STDLIB_DIR = Path(__file__).parent / "stdlib"
META_KEY = "stdlib"
ENV_STDLIB = "DAWG_REPLAY_STDLIB"  # "off": не ставить (тесты, где библиотека мешает)


def bundled() -> list[Path]:
    return sorted(STDLIB_DIR.glob("*.py"))


def fingerprint(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def stored_fingerprint(app: App) -> str | None:
    row = app.db.execute("SELECT value FROM app_meta WHERE key = ?", (META_KEY,)).fetchone()
    return row["value"] if row else None


def enabled() -> bool:
    return os.environ.get(ENV_STDLIB, "").strip().lower() not in ("off", "0", "no", "false")


def sync(app: App, force: bool = False) -> dict[str, Any] | None:
    """Ставит узлы библиотеки. None, если ставить нечего (выключено или отпечаток тот же).

    Итог: {"added": [...], "updated": [...], "unchanged": [...], "skipped": [{"name", "reason"}]}.
    """
    if not enabled():
        return None
    files = bundled()
    fp = fingerprint(files)
    if not force and stored_fingerprint(app) == fp:
        return None
    report: dict[str, Any] = {"added": [], "updated": [], "unchanged": [], "skipped": []}
    for f in files:
        _install(app, f, report)
    app.db.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (META_KEY, fp),
    )
    return report


def _install(app: App, file: Path, report: dict[str, Any]) -> None:
    data = file.read_bytes()
    label = f"встроенный узел {file.name}"
    name = _name_of(data)
    row = app.db.execute("SELECT builtin FROM scripts WHERE name = ?", (name,)).fetchone() if name else None
    if row is not None and not row["builtin"]:
        report["skipped"].append({"name": name, "reason": "есть скрипт пользователя с тем же name, он не трогается"})
        return
    try:
        chk = app.registry.check(data, label, builtin=True)
    except ReplayError as e:
        if e.code in ("version_exists", "version_too_low") and name and _use_known(app, name, data):
            report["unchanged"].append(name)  # откат приложения: такая версия уже есть, она и текущая
            return
        report["skipped"].append({"name": name or file.stem, "reason": e.message})
        return
    name = chk.manifest.name
    if chk.status == "unchanged":
        _use_known(app, name, data)  # та же версия есть, но текущей могла быть другая
        report["unchanged"].append(name)
        return
    try:
        app.registry.add_bytes(data, label, builtin=True)
    except (ReplayError, sqlite3.IntegrityError) as e:
        # Другой процесс успел поставить ту же версию одновременно с нами.
        if _use_known(app, name, data):
            report["unchanged"].append(name)
        else:
            report["skipped"].append({"name": name, "reason": getattr(e, "message", str(e))})
        return
    report["updated" if row is not None else "added"].append(name)


def _name_of(data: bytes) -> str | None:
    from dawg_replay_kit.manifest import ManifestError, parse_manifest

    try:
        return parse_manifest(data.decode("utf-8-sig")).name
    except (ManifestError, UnicodeDecodeError):
        return None


def _use_known(app: App, name: str, data: bytes) -> bool:
    """Если у встроенного узла уже есть версия с этим содержимым, делает её текущей."""
    sha = hashlib.sha256(data).hexdigest()
    row = app.db.execute(
        "SELECT v.version, s.current_version, s.builtin FROM script_versions v JOIN scripts s ON s.name = v.name"
        " WHERE v.name = ? AND v.sha256 = ? ORDER BY v.version DESC LIMIT 1",
        (name, sha),
    ).fetchone()
    if row is None or not row["builtin"]:
        return False
    if row["current_version"] != row["version"]:
        app.registry.rollback(name, row["version"])
    return True

