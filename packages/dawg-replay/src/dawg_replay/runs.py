"""Таблица запусков: запись, чтение, статусы."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError

STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"  # ждёт свободного слота (плановый запуск или run --wait)
FINAL_STATUSES = ("ok", "partial", "failed")

REASON_LOST = "lost"
REASON_START_ERROR = "start_error"
REASON_SKIPPED = "skipped"  # не дождался слота за отведённое время
REASON_CANCELLED = "cancelled"
REASON_NOT_STARTED = "not_started"  # плановый запуск упал до создания запуска (параметры, пресет)
_LABELLED_REASONS = (REASON_CANCELLED, "timeout", REASON_LOST, REASON_SKIPPED, REASON_NOT_STARTED)


def _trigger(schedule_id: str | None) -> str | None:
    """runs.triggered_by: NULL вручную, schedule по расписанию."""
    return "schedule" if schedule_id else None


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
    notify: str | None = None  # выбор на форме запуска: None по настройкам, always, never
    notify_state: str | None = None  # сообщение об итоге в Telegram, см. dawg_replay.tg_notify
    source_run: str | None = None  # запуск, после которого начат этот
    inputs: list[dict[str, Any]] = field(default_factory=list)  # откуда взяты файлы параметров, см. transfer

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING

    @property
    def waiting(self) -> bool:
        return self.status == STATUS_WAITING

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
            "notify": self.notify,
            "source_run": self.source_run,
            "inputs": self.inputs,
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
        notify=row["notify"],
        notify_state=row["notify_state"],
        source_run=row["source_run"],
        inputs=json.loads(row["inputs_json"]) if row["inputs_json"] else [],
    )


WAITER_LOST = "процесс, ждавший свободного слота, пропал"
WAITER_STALE_SECONDS = 60  # без времени старта процесса: ждущий мёртв, если не отмечался столько


def waiter_process_alive(rec: Run) -> bool:
    """Процесс, ждущий слота, жив. Чужой процесс с тем же pid живым не считается."""
    from dawg_replay import procinfo

    if rec.supervisor_pid and rec.supervisor_started is not None:
        return procinfo.same_process(rec.supervisor_pid, rec.supervisor_started)
    ts = parse_ts(rec.heartbeat_at) or parse_ts(rec.created_at)
    return ts is not None and (datetime.now(timezone.utc) - ts).total_seconds() < WAITER_STALE_SECONDS


@dataclass
class WaitPolicy:
    """Как ждать слота, если лимит параллельных запусков занят.

    Очередь честная: свободный слот получает ждущий, который встал раньше (порядок
    вставки строки, rowid). Каждая проверка идёт под BEGIN IMMEDIATE, поэтому два
    ждущих процесса не займут один слот.
    """

    seconds: float
    poll: float = 2.0
    on_wait: Callable[[dict[str, Any]], None] | None = None  # при смене позиции в очереди и при старте
    pid: int = field(default_factory=os.getpid)
    pid_started: float | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


class RunStore:
    def __init__(self, conn: sqlite3.Connection, waiter_alive: Callable[[Run], bool] | None = None):
        self._db = conn
        # Жив ли процесс, который ждёт слота. Мёртвый ждущий место в очереди не держит.
        self.waiter_alive: Callable[[Run], bool] = waiter_alive or waiter_process_alive

    def _live_waiters(self, before_rowid: int | None = None) -> list[str]:
        """id ждущих в порядке очереди, мёртвые закрываются как lost. Вызывать внутри транзакции."""
        rows = self._db.execute("SELECT rowid, * FROM runs WHERE status = ? ORDER BY rowid", (STATUS_WAITING,)).fetchall()
        out = []
        for row in rows:
            if before_rowid is not None and row["rowid"] >= before_rowid:
                break
            rec = _run(row)
            if self.waiter_alive(rec):
                out.append(rec.id)
            else:
                self.close_waiting(rec.id, REASON_LOST, WAITER_LOST)
        return out

    def _running_ids(self) -> list[str]:
        rows = self._db.execute("SELECT id FROM runs WHERE status = ? ORDER BY created_at", (STATUS_RUNNING,))
        return [r["id"] for r in rows]

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
        wait: WaitPolicy | None = None,
        notify: str | None = None,
        source_run: str | None = None,
    ) -> Run:
        """Создаёт запись `running`, если не превышен лимит параллельных запусков.

        Проверка и вставка идут в одной транзакции, два CLI одновременно лимит не обойдут.
        Ждущие слота (`waiting`) занимают место раньше новых. Без `wait` при занятом
        лимите отказ сразу. С `wait` запись встаёт в очередь `waiting` и ждёт до
        `wait.seconds`; не дождалась: `failed (skipped)` и ReplayError `run_skipped`.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            active = self._running_ids()
            queued = self._live_waiters()
            free = len(active) + len(queued) < limit
            if not free and wait is None:
                queue = f", в очереди ждут ещё {len(queued)}" if queued else ""
                raise ReplayError(
                    "parallel_limit",
                    f"уже идут {len(active)} запуска(ов) при лимите {limit}: {', '.join(active)}{queue}. "
                    "Дождитесь завершения или отмените лишний (dawg-replay cancel <run_id>); "
                    "поднять лимит: dawg-replay config set max_parallel_runs <N>",
                    {"running": active, "waiting": queued, "limit": limit},
                )
            self._db.execute(
                "INSERT INTO runs (id, script_name, script_version, script_path, draft, preset, status,"
                " params_json, created_at, timeout_seconds, triggered_by, schedule_id, notify, source_run)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    script_name,
                    script_version,
                    script_path,
                    int(draft),
                    preset,
                    STATUS_RUNNING if free else STATUS_WAITING,
                    json.dumps(params, ensure_ascii=False),
                    utcnow(),
                    timeout_seconds,
                    _trigger(schedule_id),
                    schedule_id,
                    notify,
                    source_run,
                ),
            )
            if not free:
                self._db.execute(
                    "UPDATE runs SET supervisor_pid = ?, supervisor_started = ?, heartbeat_at = ? WHERE id = ?",
                    (wait.pid, wait.pid_started, utcnow(), run_id),
                )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        if free:
            return self.get(run_id)
        try:
            return self._wait_for_slot(run_id, limit, wait)
        except BaseException as e:
            # Ctrl+C или сбой во время ожидания: место в очереди освобождается сразу.
            if not isinstance(e, ReplayError):
                self.cancel_waiting(run_id, "ожидание слота прервано")
            raise

    def _wait_for_slot(self, run_id: str, limit: int, wait: WaitPolicy) -> Run:
        started = wait.clock()
        deadline = started + wait.seconds
        rowid = self._db.execute("SELECT rowid FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
        last: tuple[int, int] | None = None
        while True:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rec = self.get(run_id)
                if rec.status != STATUS_WAITING:  # отменили из UI или CLI
                    self._db.execute("COMMIT")
                    raise ReplayError("run_cancelled", f"запуск {run_id} отменён, пока ждал слота", rec.to_dict())
                active = self._running_ids()
                ahead = self._live_waiters(before_rowid=rowid)
                if len(active) + len(ahead) < limit:
                    self._db.execute(
                        "UPDATE runs SET status = ?, heartbeat_at = ? WHERE id = ? AND status = ?",
                        (STATUS_RUNNING, utcnow(), run_id, STATUS_WAITING),
                    )
                    self._db.execute("COMMIT")
                    if wait.on_wait is not None:
                        wait.on_wait({"state": "started", "waited_seconds": wait.clock() - started})
                    return self.get(run_id)
                if wait.clock() >= deadline:
                    msg = (
                        f"пропущен: за {round(wait.seconds / 60)} мин не освободился слот "
                        f"(лимит {limit}, шли {', '.join(active) or 'нет'})"
                    )
                    self._db.execute(
                        "UPDATE runs SET status = 'failed', reason = ?, error = ?, finished_at = ? WHERE id = ? AND status = ?",
                        (REASON_SKIPPED, msg, utcnow(), run_id, STATUS_WAITING),
                    )
                    self._db.execute("COMMIT")
                    raise ReplayError("run_skipped", msg, {"run": run_id, "running": active, "limit": limit})
                self._db.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (utcnow(), run_id))
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            if wait.on_wait is not None and (len(active), len(ahead)) != last:
                last = (len(active), len(ahead))
                wait.on_wait({"state": "waiting", "running": len(active), "ahead": len(ahead), "limit": limit,
                              "left_seconds": max(0.0, deadline - wait.clock())})
            wait.sleep(wait.poll)

    def record_not_started(
        self, *, run_id: str, script_name: str, script_version: int | None, script_path: str,
        preset: str | None, schedule_id: str | None, error: str,
        source_run: str | None = None, reason: str = REASON_NOT_STARTED,
    ) -> Run:
        """Запись о плановом запуске, который не начался: без неё причина есть только в логе
        расписания. `reason`: not_started или skipped (срок ожидания вышел)."""
        now = utcnow()
        self._db.execute(
            "INSERT INTO runs (id, script_name, script_version, script_path, draft, preset, status, reason, error,"
            " params_json, created_at, finished_at, triggered_by, schedule_id, source_run)"
            " VALUES (?, ?, ?, ?, 0, ?, 'failed', ?, ?, '{}', ?, ?, ?, ?, ?)",
            (run_id, script_name, script_version, script_path, preset, reason, error, now, now,
             _trigger(schedule_id), schedule_id, source_run),
        )
        return self.get(run_id)

    def set_inputs(self, run_id: str, inputs: list[dict[str, Any]]) -> None:
        """Снимок: из каких запусков и файлов взяты значения параметров (ссылки run:/last:, шаги проекта)."""
        self._db.execute("UPDATE runs SET inputs_json = ? WHERE id = ?",
                         (json.dumps(inputs, ensure_ascii=False) if inputs else None, run_id))

    def fed_by(self, run_id: str, limit: int = 50) -> list[Run]:
        """Запуски, начатые после этого или взявшие его файлы, новые первыми."""
        rows = self._db.execute(
            "SELECT * FROM runs WHERE id != ? AND (source_run = ? OR inputs_json LIKE ?)"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (run_id, run_id, f"%{run_id}%", limit),
        )
        out = []
        for r in map(_run, rows):
            if r.source_run == run_id or any(isinstance(e, dict) and e.get("run") == run_id for e in r.inputs):
                out.append(r)
        return out

    def last_finished(self, script: str, statuses: tuple[str, ...], *, real_only: bool = True) -> Run | None:
        """Последний завершённый запуск скрипта с одним из статусов. `real_only`: только запуски,
        которые действительно шли (без not_started, skipped, start_error и отмены пользователем)."""
        marks = ", ".join("?" * len(statuses))
        sql = f"SELECT * FROM runs WHERE script_name = ? AND draft = 0 AND status IN ({marks})"
        args: list[Any] = [script, *statuses]
        if real_only:
            sql += " AND COALESCE(reason, '') NOT IN (?, ?, ?, ?)"
            args += [REASON_NOT_STARTED, REASON_SKIPPED, REASON_CANCELLED, REASON_START_ERROR]
        row = self._db.execute(sql + " ORDER BY finished_at DESC, rowid DESC LIMIT 1", args).fetchone()
        return _run(row) if row else None

    def close_waiting(self, run_id: str, reason: str, error: str) -> bool:
        """Закрывает ждущий запуск как `failed (<reason>)`. True, если он ещё ждал."""
        cur = self._db.execute(
            "UPDATE runs SET status = 'failed', reason = ?, error = ?, finished_at = ? WHERE id = ? AND status = ?",
            (reason, error, utcnow(), run_id, STATUS_WAITING),
        )
        return cur.rowcount == 1

    def cancel_waiting(self, run_id: str, error: str = "отменён, пока ждал слота") -> bool:
        return self.close_waiting(run_id, REASON_CANCELLED, error)

    def waiting(self) -> list[Run]:
        rows = self._db.execute("SELECT * FROM runs WHERE status = ? ORDER BY rowid", (STATUS_WAITING,))
        return [_run(r) for r in rows]

    def get(self, run_id: str) -> Run:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ReplayError("run_not_found", f"запуска `{run_id}` нет; список: dawg-replay runs")
        return _run(row)

    def list(self, script: str | None = None, limit: int = 20) -> list[Run]:
        if script is None:
            rows = self._db.execute("SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        else:
            rows = self._db.execute(
                "SELECT * FROM runs WHERE script_name = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (script, limit)
            )
        return [_run(r) for r in rows]

    def created_since(self, since: str, script: str | None = None) -> list[Run]:
        """Запуски, созданные не раньше `since` (ISO UTC с Z), от новых к старым."""
        sql, args = "SELECT * FROM runs WHERE created_at >= ?", [since]
        if script is not None:
            sql, args = sql + " AND script_name = ?", [*args, script]
        rows = self._db.execute(sql + " ORDER BY created_at DESC, rowid DESC", args)
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

    def set_params(self, run_id: str, params: dict[str, Any]) -> None:
        """Значения в истории после подготовки (файлы параметров `file` уже скопированы в каталог запуска)."""
        self._db.execute("UPDATE runs SET params_json = ? WHERE id = ?", (json.dumps(params, ensure_ascii=False), run_id))

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
