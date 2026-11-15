"""Таблицы параметров `table`, загруженные через форму, до запуска или сохранения пресета.

Файл разбирается сразу при загрузке (dawg_replay_kit.tables), в форме остаётся
только id загрузки. Разобранная таблица лежит в `<home>/uploads/<id>.json`
(каталог 0700, файл 0600) и удаляется через сутки. Так секретная таблица не
попадает в страницу и не ходит туда-обратно через браузер.

    up = Uploads(app.paths.uploads)
    uid = up.put("accounts", "Аккаунты.xlsx", rows, secret=False)
    table = up.get(uid)        # {"param", "file", "rows", "secret"} или None

Секретная таблица в историю запуска не попадает. Чтобы перезапустить упавшие строки
(dr-63), запуск помнит, из какой загрузки взята его секретная таблица:
`link(run_id, {"accounts": uid})` пишет `<home>/uploads/runs/<run_id>.json`. Ссылка
живёт, пока жива загрузка; потом таблицу нужно загрузить заново.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

MAX_BYTES = 10_000_000  # 10 МБ: таблица на 10 000 строк в XLSX заметно меньше
TTL_SECONDS = 24 * 3600
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_RUN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def is_upload_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


class Uploads:
    def __init__(self, base: Path, now: Any = time.time):
        self.dir = base
        self._now = now

    def put(self, param: str, file: str, rows: list[dict[str, Any]], *, secret: bool) -> str:
        self.cleanup()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        uid = secrets.token_urlsafe(16)
        path = self._path(uid)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"param": param, "file": file, "rows": rows, "secret": secret}, f, ensure_ascii=False)
        tmp.replace(path)
        return uid

    def get(self, uid: str) -> dict[str, Any] | None:
        if not is_upload_id(uid):
            return None
        path = self._path(uid)
        try:
            if self._now() - path.stat().st_mtime > TTL_SECONDS:
                path.unlink(missing_ok=True)
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("rows"), list) else None

    def link(self, run_id: str, tables: dict[str, str]) -> None:
        """Запуск взял секретные таблицы из этих загрузок (параметр -> id загрузки)."""
        tables = {k: v for k, v in tables.items() if is_upload_id(v)}
        if not tables or not _RUN_RE.fullmatch(run_id):
            return
        d = self.dir / "runs"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        os.chmod(d, 0o700)
        path = d / f"{run_id}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tables, f)
        tmp.replace(path)

    def linked(self, run_id: str, param: str) -> dict[str, Any] | None:
        """Секретная таблица запуска из живой загрузки или None (не было ссылки, загрузка устарела)."""
        if not _RUN_RE.fullmatch(run_id):
            return None
        try:
            tables = json.loads((self.dir / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        uid = tables.get(param) if isinstance(tables, dict) else None
        data = self.get(uid) if isinstance(uid, str) else None
        return data if data is not None and data.get("param") == param else None

    def cleanup(self) -> None:
        if not self.dir.is_dir():
            return
        limit = self._now() - TTL_SECONDS
        runs = self.dir / "runs"
        if runs.is_dir():
            for f in runs.iterdir():
                try:
                    if f.stat().st_mtime < limit:
                        f.unlink()
                except OSError:
                    pass
        for f in self.dir.iterdir():
            if f.is_dir():
                continue
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass

    def _path(self, uid: str) -> Path:
        return self.dir / f"{uid}.json"
