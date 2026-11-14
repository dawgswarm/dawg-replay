"""Импорт скрипта через интерфейс: загрузка, карточка перед добавлением, добавление.

Проверки те же, что у `dawg-replay add` (Registry.check), здесь только хранение
загруженного файла между шагами. Файл лежит в `<home>/imports/<id>.py`: id случайный
и одноразовый, через час файл удаляется. Имя файла из браузера в путь не попадает,
оно только показывается в тексте ошибок.

    imp = Imports(app)
    import_id = imp.stage(data, "script.py")   # шаг 1: проверка; ошибки как у add
    card = imp.preview(import_id)               # шаг 2: карточка и отличия от текущей версии
    result = imp.commit(import_id)              # шаг 3: регистрация, файл импорта удаляется
"""

from __future__ import annotations

import difflib
import os
import re
import secrets
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from dawg_replay_kit.manifest import Manifest, Param

from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from dawg_replay.registry import AddResult, Check

MAX_BYTES = 1_000_000  # 1 МБ: скрипт сбора данных намного меньше
TTL_SECONDS = 3600
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_LABEL = "загруженный файл"
EXPIRED = "Загруженный файл не найден или устарел (он хранится час). Загрузите скрипт заново."


def display_name(filename: str | None) -> str:
    """Имя файла из браузера только для текста: без каталогов и управляющих символов."""
    if not filename:
        return _LABEL
    name = PureWindowsPath(PurePosixPath(filename).name).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:100]
    return name or _LABEL


class Imports:
    def __init__(self, app: App, now: Any = time.time):
        self.app = app
        self._now = now
        self.dir: Path = app.paths.imports

    # --- шаги ---

    def stage(self, data: bytes, filename: str | None) -> str:
        """Проверяет файл как add и сохраняет его до добавления. Возвращает id импорта."""
        self.cleanup()
        label = display_name(filename)
        if filename is not None and not label.lower().endswith(".py"):
            raise ReplayError("not_python", f"«{label}» не скрипт: нужен один файл .py")
        if not data:
            raise ReplayError("empty_file", f"«{label}» пустой")
        if len(data) > MAX_BYTES:
            raise ReplayError("file_too_large", f"«{label}» больше 1 МБ, это не похоже на скрипт dawg-replay")
        self.app.registry.check(data, label)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        import_id = secrets.token_urlsafe(16)
        path = self._path(import_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return import_id

    def preview(self, import_id: str) -> dict[str, Any]:
        """Карточка перед добавлением. Проверка повторяется: реестр мог измениться."""
        data = self.load(import_id)
        chk = self.app.registry.check(data, _LABEL)
        m = chk.manifest
        card: dict[str, Any] = {
            "id": import_id,
            "status": chk.status,
            "manifest": m.to_dict(),
            "site": m.site,
            "tags": list(m.tags),
            "requires_python": m.requires_python,
            "dependencies": list(m.dependencies),
            "previous_version": chk.previous_version,
            "params_diff": None,
            "code_diff": None,
        }
        if chk.previous_version is not None and chk.status == "added":
            card.update(self._compare(m, chk))
        return card

    def commit(self, import_id: str) -> AddResult:
        data = self.load(import_id)
        result = self.app.registry.add_bytes(data, _LABEL)
        self.discard(import_id)
        return result

    def discard(self, import_id: str) -> None:
        if _ID_RE.fullmatch(import_id or ""):
            self._path(import_id).unlink(missing_ok=True)

    # --- хранение ---

    def load(self, import_id: str) -> bytes:
        if not _ID_RE.fullmatch(import_id or ""):
            raise ReplayError("import_not_found", EXPIRED)
        path = self._path(import_id)
        try:
            if self._now() - path.stat().st_mtime > TTL_SECONDS:
                path.unlink(missing_ok=True)
                raise ReplayError("import_not_found", EXPIRED)
            return path.read_bytes()
        except FileNotFoundError:
            raise ReplayError("import_not_found", EXPIRED) from None

    def cleanup(self) -> None:
        """Удаляет устаревшие загрузки (и недописанные .tmp)."""
        if not self.dir.is_dir():
            return
        limit = self._now() - TTL_SECONDS
        for f in self.dir.iterdir():
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass

    def _path(self, import_id: str) -> Path:
        return self.dir / f"{import_id}.py"

    # --- отличия от текущей версии ---

    def _compare(self, new: Manifest, chk: Check) -> dict[str, Any]:
        name = new.name
        current = self.app.registry.version(name)
        try:
            old_text = current.path.read_text(encoding="utf-8-sig")
        except OSError:
            old_text = ""
        try:
            old = self.app.registry.manifest(name)
        except ReplayError:
            old = None
        diff = difflib.unified_diff(
            old_text.splitlines(keepends=True),
            chk.text.splitlines(keepends=True),
            fromfile=f"версия {current.version}",
            tofile=f"версия {new.version}",
        )
        return {
            "params_diff": params_diff(old.params if old else (), new.params),
            "code_diff": "".join(diff),
        }


_PARAM_FIELDS = {
    "type": "тип",
    "title": "название",
    "description": "описание",
    "required": "обязательный",
    "default": "по умолчанию",
    "min": "минимум",
    "max": "максимум",
    "max_items": "не больше элементов",
    "choices": "варианты",
}


def params_diff(old: tuple[Param, ...], new: tuple[Param, ...]) -> dict[str, list]:
    """Добавленные, удалённые и изменённые параметры. У изменённых список «поле: было → стало»."""
    before = {p.name: p.to_dict() for p in old}
    after = {p.name: p.to_dict() for p in new}
    changed = []
    for name in after:
        if name not in before or before[name] == after[name]:
            continue
        what = []
        for key, title in _PARAM_FIELDS.items():
            a, b = before[name].get(key), after[name].get(key)
            if a != b:
                what.append(f"{title}: {_show(a)} → {_show(b)}")
        changed.append({"name": name, "title": after[name]["title"], "changes": what})
    return {
        "added": [{"name": n, "title": after[n]["title"], "type": after[n]["type"]} for n in after if n not in before],
        "removed": [{"name": n, "title": before[n]["title"], "type": before[n]["type"]} for n in before if n not in after],
        "changed": changed,
    }


def _show(value: Any) -> str:
    if value is None:
        return "нет"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "пусто"
    return str(value)
