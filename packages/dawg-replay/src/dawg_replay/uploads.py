"""Таблицы параметров `table` и файлы параметров `file`, загруженные через форму,
до запуска или сохранения пресета.

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

    def put_file(self, param: str, name: str, data: bytes, *, secret: bool) -> str:
        """Файл параметра `file` (dr-84): байты в `<id>.bin`, описание в `<id>.json`, оба 0600."""
        self.cleanup()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        uid = secrets.token_urlsafe(16)
        _write_private(self.dir / f"{uid}.bin", data)
        meta = {"param": param, "file": name, "size": len(data), "secret": secret, "kind": "file"}
        _write_private(self._path(uid), json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        return uid

    def get_file(self, uid: str) -> dict[str, Any] | None:
        """{"param", "file", "size", "secret", "path"} загруженного файла или None (нет, устарел)."""
        if not is_upload_id(uid):
            return None
        meta_path, bin_path = self._path(uid), self.dir / f"{uid}.bin"
        try:
            if self._now() - meta_path.stat().st_mtime > TTL_SECONDS:
                meta_path.unlink(missing_ok=True)
                bin_path.unlink(missing_ok=True)
                return None
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("kind") != "file" or not bin_path.is_file():
            return None
        return {**data, "path": str(bin_path)}

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

    def put_source(self, uid: str, data: bytes) -> None:
        """Исходный XLSX загрузки: из него форма перечитывает другой лист. Та же защита и срок, что у таблицы."""
        if not is_upload_id(uid):
            return
        path = self.dir / f"{uid}.src"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    def source(self, uid: str) -> bytes | None:
        if not is_upload_id(uid) or self.get(uid) is None:
            return None
        try:
            return (self.dir / f"{uid}.src").read_bytes()
        except OSError:
            return None

    def link(self, run_id: str, tables: dict[str, Any]) -> None:
        """Запуск взял секретные таблицы и файлы из этих загрузок (параметр -> id или список id у
        `multiple`). Ссылки дописываются к прежним: запуск с таблицей и файлом связывают в двух местах."""
        tables = {k: v for k, v in tables.items()
                  if is_upload_id(v) or (isinstance(v, list) and v and all(is_upload_id(x) for x in v))}
        if not tables or not _RUN_RE.fullmatch(run_id):
            return
        d = self.dir / "runs"
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        os.chmod(d, 0o700)
        path = d / f"{run_id}.json"
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        merged = {**(old if isinstance(old, dict) else {}), **tables}
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f)
        tmp.replace(path)

    def linked_files(self, run_id: str, param: str) -> list[dict[str, Any]] | None:
        """Секретные файлы запуска из живых загрузок: [{"path", "name", "size"}] или None
        (ссылки нет или хоть один файл устарел: тогда файл нужно загрузить заново)."""
        if not _RUN_RE.fullmatch(run_id):
            return None
        try:
            links = json.loads((self.dir / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        uids = links.get(param) if isinstance(links, dict) else None
        uids = [uids] if isinstance(uids, str) else uids
        if not isinstance(uids, list) or not uids:
            return None
        out = []
        for uid in uids:
            data = self.get_file(uid) if isinstance(uid, str) else None
            if data is None or data.get("param") != param:
                return None
            out.append({"path": data["path"], "name": data["file"], "size": data["size"]})
        return out

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


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    tmp.replace(path)
