"""Таблица запусков: запись, чтение, статусы."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError

STATUS_RUNNING = "running"
FINAL_STATUSES = ("ok", "partial", "failed")

REASON_LOST = "lost"
REASON_START_ERROR = "start_error"
_LABELLED_REASONS = ("cancelled", "timeout", REASON_LOST)


def new_run_id() -> str:
    """Сортируется по времени, уникален: `20261114-153012-a1b2c3`."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Run:
    id: str
    script_name: str
    script_version: int | None
    script_path: str
    draft: bool
    preset: str | None
    status: str
    reason: str | None
    params: dict[str, Any]
    created_at: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    pid: int | None
    pid_started: float | None
    supervisor_pid: int | None
    supervisor_started: float | None
    summary: str | None
    error: str | None
    killed: bool
    timeout_seconds: int | None
    heartbeat_at: str | None
    cancel_requested_at: str | None
    trigger: str = "manual"  # manual или schedule
    schedule_id: str | None = None

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING

    @property
    def label(self) -> str:
        """`ok`, `partial`, `failed`, `failed (timeout)`, `failed (cancelled)`, `failed (lost)`, `running`."""
        if self.reason in _LABELLED_REASONS:
            return f"{self.status} ({self.reason})"
        return self.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "script": self.script_name,
            "version": self.script_version,
            "script_path": self.script_path,
            "draft": self.draft,
            "preset": self.preset,
            "status": self.status,
            "reason": self.reason,
            "label": self.label,
            "params": self.params,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "pid": self.pid,
            "summary": self.summary,
            "error": self.error,
            "killed": self.killed,
            "timeout_seconds": self.timeout_seconds,
            "cancel_requested": self.cancel_requested_at is not None,
            "trigger": self.trigger,
            "schedule_id": self.schedule_id,
        }


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        script_name=row["script_name"],
        script_version=row["script_version"],
        script_path=row["script_path"],
        draft=bool(row["draft"]),
        preset=row["preset"],
        status=row["status"],
        reason=row["reason"],
        params=json.loads(row["params_json"]) if row["params_json"] else {},
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        exit_code=row["exit_code"],
        pid=row["pid"],
        pid_started=row["pid_started"],
        supervisor_pid=row["supervisor_pid"],
        supervisor_started=row["supervisor_started"],
        summary=row["summary"],
        error=row["error"],
        killed=bool(row["killed"]),
        timeout_seconds=row["timeout_seconds"],
        heartbeat_at=row["heartbeat_at"],
        cancel_requested_at=row["cancel_requested_at"],
        trigger=row["triggered_by"] or "manual",
        schedule_id=row["schedule_id"],
    )


class RunStore:
    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    def reserve(
        self,
        *,
        run_id: str,
        script_name: str,
        script_version: int | None,
        script_path: str,
        draft: bool,
        preset: str | None,
        params: dict[str, Any],
        timeout_seconds: int,
        limit: int,
        schedule_id: str | None = None,
    ) -> Run:
        """Создаёт запись `running`, если не превышен лимит параллельных запусков.

        Проверка и вставка идут в одной транзакции, два CLI одновременно лимит не обойдут.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            active = [r["id"] for r in self._db.execute(
                "SELECT id FROM runs WHERE status = ? ORDER BY created_at", (STATUS_RUNNING,)
            )]
            if len(active) >= limit:
                raise ReplayError(
                    "parallel_limit",
                    f"уже идут {len(active)} запуска(ов) при лимите {limit}: {', '.join(active)}. "
                    "Дождитесь завершения или отмените лишний (dawg-replay cancel <run_id>); "
                    "поднять лимит: dawg-replay config set max_parallel_runs <N>",
                    {"running": active, "limit": limit},
                )
            self._db.execute(
                "INSERT INTO runs (id, script_name, script_version, script_path, draft, preset, status,"
                " params_json, created_at, timeout_seconds, triggered_by, schedule_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    script_name,
                    script_version,
                    script_path,
                    int(draft),
                    preset,
                    STATUS_RUNNING,
                    json.dumps(params, ensure_ascii=False),
                    utcnow(),
                    timeout_seconds,
                    "schedule" if schedule_id else None,
                    schedule_id,
                ),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return self.get(run_id)

    def get(self, run_id: str) -> Run:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ReplayError("run_not_found", f"запуска `{run_id}` нет; список: dawg-replay runs")
        return _run(row)

    def list(self, script: str | None = None, limit: int = 20) -> list[Run]:
        if script is None:
            rows = self._db.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        else:
            rows = self._db.execute(
                "SELECT * FROM runs WHERE script_name = ? ORDER BY created_at DESC, id DESC LIMIT ?", (script, limit)
            )
        return [_run(r) for r in rows]

    def created_since(self, since: str, script: str | None = None) -> list[Run]:
        """Запуски, созданные не раньше `since` (ISO UTC с Z), от новых к старым."""
        sql, args = "SELECT * FROM runs WHERE created_at >= ?", [since]
        if script is not None:
            sql, args = sql + " AND script_name = ?", [*args, script]
        rows = self._db.execute(sql + " ORDER BY created_at DESC, id DESC", args)
        return [_run(r) for r in rows]

    def running(self) -> list[Run]:
        rows = self._db.execute("SELECT * FROM runs WHERE status = ? ORDER BY created_at", (STATUS_RUNNING,))
        return [_run(r) for r in rows]

    def set_supervisor(self, run_id: str, pid: int, started: float | None) -> None:
        self._db.execute(
            "UPDATE runs SET supervisor_pid = ?, supervisor_started = COALESCE(?, supervisor_started), heartbeat_at = ?"
            " WHERE id = ?",
            (pid, started, utcnow(), run_id),
        )

    def set_started(self, run_id: str, pid: int, started: float | None) -> None:
        now = utcnow()
        self._db.execute(
            "UPDATE runs SET pid = ?, pid_started = ?, started_at = ?, heartbeat_at = ? WHERE id = ?",
            (pid, started, now, now, run_id),
        )

    def heartbeat(self, run_id: str) -> None:
        self._db.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (utcnow(), run_id))

    def request_cancel(self, run_id: str) -> None:
        self._db.execute(
            "UPDATE runs SET cancel_requested_at = COALESCE(cancel_requested_at, ?) WHERE id = ?",
            (utcnow(), run_id),
        )

    def cancel_requested(self, run_id: str) -> bool:
        row = self._db.execute("SELECT cancel_requested_at FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row is not None and row[0] is not None

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        reason: str | None,
        exit_code: int | None = None,
        summary: str | None = None,
        killed: bool = False,
        error: str | None = None,
        finished_at: str | None = None,
    ) -> bool:
        """Ставит итог. Срабатывает только для запуска в статусе `running`: True, если итог поставили мы."""
        cur = self._db.execute(
            "UPDATE runs SET status = ?, reason = ?, exit_code = ?, summary = ?, killed = ?, error = ?,"
            " finished_at = ? WHERE id = ? AND status = ?",
            (status, reason, exit_code, summary, int(killed), error, finished_at or utcnow(), run_id, STATUS_RUNNING),
        )
        return cur.rowcount == 1

    def add_error(self, run_id: str, error: str) -> None:
        self._db.execute(
            "UPDATE runs SET error = CASE WHEN error IS NULL THEN ? ELSE error || char(10) || ? END WHERE id = ?",
            (error, error, run_id),
        )
