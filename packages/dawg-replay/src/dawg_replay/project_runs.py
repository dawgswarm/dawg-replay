"""Запуск проекта: шаги по схеме, файлы по стрелкам, общий статус (M6, спека 2026-11-17-m6-projects).

Запуск проекта это запись `project_runs` со своим id, версией проекта и шагами `project_steps`
(узел, статус, запуск). Шаг это обычный запуск скрипта (`runs`) с пометкой `project_run` и
`project_node`: журнал, файлы и экран запуска у него как у любого запуска.

Как идёт запуск:
- старт: узлы без входящих стрелок;
- узел решается, когда решены источники всех входящих стрелок. Стрелка активна, если источник
  реально шёл, его исход подходит под `when` и (с `if_file`) он отдал этот файл;
- `wait = "all"`: узел запускается, если активны все входящие; `any`: если хоть одна.
  Иначе «пропущен» (неактивна из-за исхода источника) или «не по этой ветке» (из-за `if_file`
  или потому, что источники сами не по этой ветке);
- файлы стрелок берутся из шагов этого же запуска проекта. Несколько стрелок в одну таблицу
  склеиваются в порядке стрелок в файле, колонки должны совпадать; `source_column` добавляет
  колонку с названием узла-источника. Значение стрелки перекрывает значение пресета и узла;
- шаги ждут места в пределах `max_parallel_runs`, как плановые запуски.

Своего демона нет. Решения принимает тот, кто закрыл шаг (`launch.supervise` и прочие места,
где запуск заканчивается, зовут `on_step_finish`). Кроме того, на время запуска проекта
поднимается процесс-ведущий (`python -m dawg_replay.project_runs --home <dir> drive <id>`):
он следит за сроком `timeout_minutes` и подбирает решения, если шаги закрылись без него.
Ведущий пропал (компьютер выключен, процесс убит): `recover`, который зовёт `launch.recover_lost`
из любой команды, решает за него и поднимает нового. Шаг идёт в своём процессе
(`... step <id> <узел>`): ждёт места, запускает скрипт и сам его ведёт, как `run --follow`.

Тесты выключают процессы переменной DAWG_REPLAY_PROJECT_PROCESSES=off и подменяют `_spawn_step`.

    pr = start(app, "wb-to-google")
    pr = get(app, pr.id)            # статус и шаги
    stop(app, pr.id)
    retry(app, pr.id)               # с упавшего, по той же версии
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay import procinfo, transfer
from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.runs import REASON_CANCELLED, REASON_NOT_STARTED, REASON_SKIPPED, REASON_START_ERROR, Run, parse_ts

if TYPE_CHECKING:
    from dawg_replay.app import App
    from dawg_replay.projects import Edge, Project

ENV_PROCESSES = "DAWG_REPLAY_PROJECT_PROCESSES"  # off: не поднимать ведущего и процессы шагов (тесты)
POLL_SECONDS = 2.0
STARTING_GRACE = timedelta(minutes=2)  # шаг «запускается», а процесса шага так и нет: не стартовал

# Статусы запуска проекта
RUNNING, OK, PARTIAL, FAILED, CANCELLED = "running", "ok", "partial", "failed", "cancelled"
REASON_TIMEOUT, REASON_LOST, REASON_BUSY = "timeout", "lost", "busy"
PROJECT_TITLES = {RUNNING: "идёт", OK: "готово", PARTIAL: "частично", FAILED: "упал", CANCELLED: "отменён"}

# Статусы шага
PENDING, STARTING, STEP_RUNNING = "pending", "starting", "running"
NOT_STARTED, SKIPPED, OFF_BRANCH = "not_started", "skipped", "off_branch"
STEP_TITLES = {
    PENDING: "ждёт", STARTING: "запускается", STEP_RUNNING: "идёт", OK: "готово", PARTIAL: "частично",
    FAILED: "упал", NOT_STARTED: "не стартовал", CANCELLED: "отменён", SKIPPED: "пропущен",
    OFF_BRANCH: "не по этой ветке",
}
ACTIVE_STEPS = (PENDING, STARTING, STEP_RUNNING)
WHEN_STATUSES = {"ok": (OK,), "partial": (OK, PARTIAL), "any": (OK, PARTIAL, FAILED)}
WHEN_WORDS = {"ok": "готово", "partial": "готово или частично", "any": "любой исход"}
OUTCOME_WORDS = {
    OK: "закончился", PARTIAL: "закончился частично", FAILED: "упал", NOT_STARTED: "не стартовал",
    CANCELLED: "отменён", SKIPPED: "пропущен",
}


@dataclass(frozen=True)
class StepRow:
    """Строка прохода по строкам: свой запуск скрипта."""

    node: str
    index: int  # порядок прохода, с 0
    status: str  # как у шага
    key: str
    label: str  # значения колонок прохода, например «Москва»
    values: dict[str, Any] = field(default_factory=dict)  # несекретные значения входов строки
    run_id: str | None = None
    reason: str | None = None
    reused: bool = False
    pid: int | None = None
    pid_started: float | None = None
    updated_at: str | None = None

    @property
    def done(self) -> bool:
        return self.status not in ACTIVE_STEPS

    @property
    def title(self) -> str:
        return STEP_TITLES.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "label": self.label, "values": self.values, "status": self.status,
                "status_title": self.title, "run_id": self.run_id, "reason": self.reason, "reused": self.reused}


def rows_summary(rows: tuple[StepRow, ...] | list[StepRow], total: int | None = None) -> dict[str, Any]:
    """Счётчик строк шага: «7 из 20, упало 1»."""
    c: dict[str, int] = {}
    for r in rows:
        c[r.status] = c.get(r.status, 0) + 1
    total = len(rows) if total is None else total
    ok = c.get(OK, 0)
    partial = c.get(PARTIAL, 0)
    failed = c.get(FAILED, 0) + c.get(NOT_STARTED, 0)
    running = c.get(STARTING, 0) + c.get(STEP_RUNNING, 0)
    text = f"{ok + partial} из {total}"
    if partial:
        text += f", частично {partial}"
    if failed:
        text += f", упало {failed}"
    rest = c.get(CANCELLED, 0) + c.get(SKIPPED, 0)
    if rest:
        text += f", не шло {rest}"
    return {"total": total, "ok": ok, "partial": partial, "failed": failed, "running": running,
            "waiting": c.get(PENDING, 0), "skipped": rest, "text": text}


@dataclass(frozen=True)
class Step:
    node: str
    pos: int
    status: str
    run_id: str | None = None  # у прохода по строкам: запуск-сборка (склеенные файлы, values.csv)
    reason: str | None = None
    reused: bool = False
    pid: int | None = None
    pid_started: float | None = None
    updated_at: str | None = None
    rows_total: int | None = None  # проход по строкам: сколько строк выбрано; None у обычного шага
    rows: tuple[StepRow, ...] = ()

    @property
    def done(self) -> bool:
        return self.status not in ACTIVE_STEPS

    @property
    def each(self) -> bool:
        return self.rows_total is not None

    @property
    def title(self) -> str:
        return STEP_TITLES.get(self.status, self.status)

    def summary(self) -> dict[str, Any] | None:
        return rows_summary(self.rows, self.rows_total) if self.each else None

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "status": self.status, "status_title": self.title, "run_id": self.run_id,
                "reason": self.reason, "reused": self.reused, "each": self.each, "rows_summary": self.summary(),
                "rows": [r.to_dict() for r in self.rows]}


@dataclass(frozen=True)
class ProjectRun:
    id: str
    project: str
    version: int
    status: str
    reason: str | None
    error: str | None
    created_at: str
    finished_at: str | None
    trigger: str  # manual или schedule
    schedule_id: str | None
    retry_of: str | None
    deadline: str | None
    cancel_requested_at: str | None
    driver_pid: int | None
    driver_started: float | None
    heartbeat_at: str | None
    notify: str | None
    notify_state: str | None
    steps: tuple[Step, ...] = field(default_factory=tuple)

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    @property
    def label(self) -> str:
        if self.status == FAILED and self.reason == REASON_TIMEOUT:
            return "упал (таймаут)"
        if self.status == FAILED and self.reason == REASON_BUSY:
            return "пропущен"
        return PROJECT_TITLES.get(self.status, self.status)

    def step(self, node: str) -> Step | None:
        return next((s for s in self.steps if s.node == node), None)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.status] = out.get(s.status, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "project": self.project, "version": self.version, "status": self.status,
            "label": self.label, "reason": self.reason, "error": self.error, "created_at": self.created_at,
            "finished_at": self.finished_at, "trigger": self.trigger, "schedule_id": self.schedule_id,
            "retry_of": self.retry_of, "deadline": self.deadline,
            "cancel_requested": self.cancel_requested_at is not None, "notify": self.notify,
            "steps": [s.to_dict() for s in self.steps],
        }


def new_id() -> str:
    """Как у запусков, с буквой p: `p20261117-090000-a1b2c3`."""
    return "p" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# --- хранение --------------------------------------------------------------------------


def _step_row(row: Any) -> StepRow:
    return StepRow(row["node"], row["idx"], row["status"], row["key"], row["label"],
                   json.loads(row["values_json"]) if row["values_json"] else {}, row["run_id"], row["reason"],
                   bool(row["reused"]), row["pid"], row["pid_started"], row["updated_at"])


def step_rows(app: App, prun_id: str, node: str) -> list[StepRow]:
    """Строки прохода шага по порядку; у обычного шага пусто."""
    return [_step_row(r) for r in app.db.execute(
        "SELECT * FROM project_step_rows WHERE project_run = ? AND node = ? ORDER BY idx", (prun_id, node))]


def _step(app: App, row: Any) -> Step:
    rows = tuple(step_rows(app, row["project_run"], row["node"])) if row["rows_total"] is not None else ()
    return Step(row["node"], row["pos"], row["status"], row["run_id"], row["reason"], bool(row["reused"]),
                row["pid"], row["pid_started"], row["updated_at"], row["rows_total"], rows)


def _row(app: App, row: Any) -> ProjectRun:
    steps = tuple(_step(app, r) for r in app.db.execute(
        "SELECT * FROM project_steps WHERE project_run = ? ORDER BY pos", (row["id"],)))
    return ProjectRun(
        row["id"], row["project"], row["version"], row["status"], row["reason"], row["error"], row["created_at"],
        row["finished_at"], row["triggered_by"] or "manual", row["schedule_id"], row["retry_of"], row["deadline"],
        row["cancel_requested_at"], row["driver_pid"], row["driver_started"], row["heartbeat_at"], row["notify"],
        row["notify_state"], steps)


def get(app: App, prun_id: str) -> ProjectRun:
    row = app.db.execute("SELECT * FROM project_runs WHERE id = ?", (prun_id,)).fetchone()
    if row is None:
        raise ReplayError("project_run_not_found", f"запуска проекта `{prun_id}` нет")
    return _row(app, row)


def history(app: App, project: str | None = None, limit: int = 20) -> list[ProjectRun]:
    if project is None:
        rows = app.db.execute("SELECT * FROM project_runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
    else:
        rows = app.db.execute("SELECT * FROM project_runs WHERE project = ? ORDER BY created_at DESC, rowid DESC"
                              " LIMIT ?", (project, limit))
    return [_row(app, r) for r in rows.fetchall()]


def running(app: App, project: str | None = None) -> list[ProjectRun]:
    if project is None:
        rows = app.db.execute("SELECT * FROM project_runs WHERE status = ? ORDER BY created_at", (RUNNING,))
    else:
        rows = app.db.execute("SELECT * FROM project_runs WHERE status = ? AND project = ? ORDER BY created_at",
                              (RUNNING, project))
    return [_row(app, r) for r in rows.fetchall()]


def _set_step(app: App, prun_id: str, node: str, status: str, *, only_from: tuple[str, ...] | None = None,
              **fields: Any) -> bool:
    """Меняет шаг; с `only_from` только из этих статусов (кто первый, тот и решил)."""
    cols = {"status": status, "updated_at": utcnow(), **fields}
    sql = "UPDATE project_steps SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE project_run = ? AND node = ?"
    args: list[Any] = [*cols.values(), prun_id, node]
    if only_from:
        sql += f" AND status IN ({', '.join('?' * len(only_from))})"
        args += list(only_from)
    return app.db.execute(sql, args).rowcount == 1


def _set_row(app: App, prun_id: str, node: str, idx: int, status: str, *, only_from: tuple[str, ...] | None = None,
             **fields: Any) -> bool:
    cols = {"status": status, "updated_at": utcnow(), **fields}
    sql = ("UPDATE project_step_rows SET " + ", ".join(f"{k} = ?" for k in cols)
           + " WHERE project_run = ? AND node = ? AND idx = ?")
    args: list[Any] = [*cols.values(), prun_id, node, idx]
    if only_from:
        sql += f" AND status IN ({', '.join('?' * len(only_from))})"
        args += list(only_from)
    return app.db.execute(sql, args).rowcount == 1


def _finish(app: App, prun_id: str, status: str, reason: str | None = None, error: str | None = None) -> bool:
    cur = app.db.execute(
        "UPDATE project_runs SET status = ?, reason = COALESCE(?, reason), error = COALESCE(?, error), finished_at = ?"
        " WHERE id = ? AND status = ?", (status, reason, error, utcnow(), prun_id, RUNNING))
    return cur.rowcount == 1


# --- узлы и стрелки --------------------------------------------------------------------


_cache: dict[tuple[str, str, int], Project] = {}


def _project(app: App, pr: ProjectRun) -> Project:
    key = (str(app.paths.home), pr.project, pr.version)
    if key not in _cache:
        _cache[key] = app.projects.load(pr.project, pr.version).project
    return _cache[key]


def node_title(app: App, project: Project, node_id: str) -> str:
    try:
        return app.projects.node_title(project.node(node_id))
    except ReplayError:
        return node_id


def _run_of(app: App, pr: ProjectRun, step: Step) -> Run | None:
    if step.run_id:
        try:
            return app.runs.get(step.run_id)
        except ReplayError:
            return None
    row = app.db.execute("SELECT id FROM runs WHERE project_run = ? AND project_node = ? AND project_row IS NULL"
                         " ORDER BY created_at DESC, rowid DESC LIMIT 1", (pr.id, step.node)).fetchone()
    return app.runs.get(row[0]) if row else None


def _row_run(app: App, prun_id: str, r: StepRow) -> Run | None:
    if r.run_id:
        try:
            return app.runs.get(r.run_id)
        except ReplayError:
            return None
    row = app.db.execute("SELECT id FROM runs WHERE project_run = ? AND project_node = ? AND project_row = ?"
                         " ORDER BY created_at DESC, rowid DESC LIMIT 1", (prun_id, r.node, r.index)).fetchone()
    return app.runs.get(row[0]) if row else None


def _short(text: str | None, limit: int = 300) -> str | None:
    """Причина одной строкой: многострочная ошибка склеивается."""
    if not text:
        return None
    one = " ".join(x.strip() for x in text.splitlines() if x.strip())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _status_of_run(rec: Run) -> str:
    if rec.status in (OK, PARTIAL):
        return rec.status
    if rec.reason == REASON_CANCELLED:
        return CANCELLED
    if rec.reason in (REASON_NOT_STARTED, REASON_SKIPPED, REASON_START_ERROR):
        return NOT_STARTED
    return FAILED


@dataclass(frozen=True)
class EdgeState:
    edge: Edge
    state: str  # active, outcome (неактивна из-за исхода источника), branch (не по этой ветке)
    text: str = ""


def edge_state(app: App, project: Project, pr: ProjectRun, edge: Edge) -> EdgeState:
    src = pr.step(edge.source)
    title = node_title(app, project, edge.source)
    if src is None:
        return EdgeState(edge, "outcome", f"{title}: шага нет")
    if src.status == OFF_BRANCH:
        return EdgeState(edge, "branch", f"{title} не по этой ветке")
    if src.status not in WHEN_STATUSES.get(edge.when, ()):
        words = OUTCOME_WORDS.get(src.status, src.status)
        if src.status in (OK, PARTIAL):
            return EdgeState(edge, "outcome", f"{title} {words}, а стрелка ждёт: {WHEN_WORDS.get(edge.when, edge.when)}")
        return EdgeState(edge, "outcome", f"{title} {words}")
    if edge.if_file:
        rec = _run_of(app, pr, src)
        if rec is None or not transfer.source_files(app, rec, edge.if_file):
            return EdgeState(edge, "branch", f"{title} не отдал {edge.if_file}")
    return EdgeState(edge, "active")


def decide(app: App, project: Project, pr: ProjectRun, node_id: str) -> tuple[str, str | None, list[Edge]] | None:
    """Решение по узлу: (start, None, активные стрелки), (skipped или off_branch, причина, []),
    или None, если не все источники решены."""
    incoming = project.incoming(node_id)
    if not incoming:
        return "start", None, []
    for e in incoming:
        src = pr.step(e.source)
        if src is not None and not src.done:
            return None
    states = [edge_state(app, project, pr, e) for e in incoming]
    active = [s.edge for s in states if s.state == "active"]
    outcome = [s.text for s in states if s.state == "outcome"]
    branch = [s.text for s in states if s.state == "branch"]
    wait = project.node(node_id).wait
    if (wait == "any" and active) or (wait != "any" and len(active) == len(states)):
        return "start", None, active
    if outcome:
        return SKIPPED, "; ".join(outcome), []
    return OFF_BRANCH, "; ".join(branch), []


# --- файлы стрелок в параметры узла -----------------------------------------------------


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    return [k for k in (rows[0] if rows else {}) if not k.startswith("_")]


def step_fills(app: App, project: Project, pr: ProjectRun, node_id: str, edges: list[Edge]) -> list[transfer.Fill]:
    """Значения параметров узла из файлов активных стрелок. Ошибка: ReplayError input_error."""
    node = project.node(node_id)
    manifest = app.registry.manifest(node.script)
    by_param: dict[str, list[tuple[Edge, Any]]] = {}
    for e in edges:
        for fm in e.files:
            by_param.setdefault(fm.param, []).append((e, fm))
    fills: list[transfer.Fill] = []
    problems: list[str] = []
    for param, sources in by_param.items():
        try:
            p = manifest.param(param)
        except KeyError:
            problems.append(f"у скрипта {node.script} нет параметра `{param}`")
            continue
        parts: list[transfer.Fill] = []
        for e, fm in sources:
            src = pr.step(e.source)
            rec = _run_of(app, pr, src) if src else None
            if rec is None:
                problems.append(f"{node_title(app, project, e.source)}: нет запуска, файл нужен параметру `{param}`")
                continue
            try:
                parts.append(transfer.fill_run(app, p, rec, fm.file, node=e.source))
            except ReplayError as err:
                problems.append(err.message)
        if not parts or len(parts) < len(sources):
            continue
        try:
            fills.append(_merge(app, project, p, sources, parts))
        except ReplayError as err:
            problems.append(err.message)
    if problems:
        text = problems[0] if len(problems) == 1 else "файлы по стрелкам не подошли:\n" + "\n".join(f"  {x}" for x in problems)
        raise ReplayError(transfer.INPUT_ERROR, text, problems)
    return fills


def _merge(app: App, project: Project, p: Any, sources: list[tuple[Edge, Any]], parts: list[transfer.Fill]) -> transfer.Fill:
    """Одна стрелка как есть; несколько: таблицы склеиваются, файлы собираются списком."""
    first = parts[0]
    with_column = [e.source_column for e, _ in sources]
    if len(parts) == 1 and not with_column[0]:
        return first
    if p.type == "file":
        if not p.multiple and len(parts) > 1:
            raise ReplayError(transfer.INPUT_ERROR, f"`{p.name}`: в один файл идут несколько стрелок")
        recs: list[Any] = []
        for f in parts:
            recs += f.value if isinstance(f.value, list) else [f.value]
        return transfer.Fill(p.name, recs, [x for f in parts for x in f.files], first.entry,
                             extra_entries=[f.entry for f in parts[1:]])
    rows: list[dict[str, Any]] = []
    head = _columns(first.value)
    for (e, _), f in zip(sources, parts):
        cols = _columns(f.value)
        if f.value and head and cols != head:
            raise ReplayError(transfer.INPUT_ERROR,
                              f"`{p.name}`: колонки таблиц не совпадают: {node_title(app, project, sources[0][0].source)}: "
                              f"{', '.join(head)}; {node_title(app, project, e.source)}: {', '.join(cols)}")
        if not head:
            head = cols
        for r in f.value:
            row = dict(r)
            if e.source_column:
                row[e.source_column] = node_title(app, project, e.source)
            rows.append(row)
    for i, r in enumerate(rows, start=2):  # номера строк общей таблицы, как у загруженной (шапка это строка 1)
        if "_row" in r:
            r["_row"] = i
    from dawg_replay_kit.manifest import check_value

    problem = check_value(p, rows)
    if problem:
        raise ReplayError(transfer.INPUT_ERROR, f"`{p.name}`: склеенная таблица не подошла: {problem}")
    return transfer.Fill(p.name, rows, [x for f in parts for x in f.files], first.entry,
                         [x for f in parts for x in f.table_files], extra_entries=[f.entry for f in parts[1:]])


# --- значения и колонки по стрелкам ------------------------------------------------------


def _read_values(app: App, run_id: str) -> dict[str, Any]:
    """Выходные значения запуска (kit 1.5, dawg_replay.run_values); секретные тоже."""
    try:
        from dawg_replay import run_values
    except ImportError:  # приложение без выходных значений
        return {}
    return run_values.read(app, run_id)


def _shown_values(app: App, run_id: str) -> list[dict[str, Any]]:
    """Выходные значения для людей: name, title, type, secret, value (у секретных None)."""
    try:
        from dawg_replay import run_values
    except ImportError:
        return []
    return run_values.shown(app, run_id)


@dataclass(frozen=True)
class RowInput:
    """Строка прохода: значения входов each этой строки."""

    index: int
    key: str  # значения ячеек JSON-ом: по нему перезапуск узнаёт строку
    label: str
    fills: list[transfer.Fill]
    shown: dict[str, Any]  # несекретные значения входов строки
    problem: str | None = None  # значение строки не подошло входу: строка не стартует


@dataclass
class Inputs:
    """Что стрелки дают узлу: общие значения и, у прохода по строкам, строки."""

    fills: list[transfer.Fill]
    unset: list[str]  # входы со стрелкой, которым значения не досталось
    rows: list[RowInput] | None = None
    edge: Edge | None = None  # стрелка прохода (parallel, on_error)
    columns: list[str] = field(default_factory=list)  # колонки прохода, для склейки таблиц


def _source_table(app: App, project: Project, rec: Run, e: Edge, spec: str) -> list[dict[str, Any]]:
    """Таблица файла `spec` источника с where и limit стрелки. Ошибка: ReplayError input_error."""
    from dawg_replay_kit.tables import TableError, parse_table

    from dawg_replay.edge_values import select

    title = node_title(app, project, e.source)
    found = transfer.source_files(app, rec, spec)
    shown = "главный файл" if spec == transfer.MAIN else spec
    if not found:
        raise ReplayError(transfer.INPUT_ERROR, f"{title}: в запуске {rec.id} нет файла {shown}")
    f = found[0]
    try:
        rows = parse_table(f.read_bytes(), f.name)
    except TableError as err:
        msg = str(err).removeprefix(f"{f.name}: ").removeprefix(f"{f.name}, ")
        raise ReplayError(transfer.INPUT_ERROR, f"{title}, {f.name}: {msg}") from None
    except OSError as err:
        raise ReplayError(transfer.INPUT_ERROR, f"{title}, {f.name}: файл не читается ({err.strerror or err})") from None
    try:
        return select(rows, e.where, e.limit)
    except ValueError as err:
        raise ReplayError(transfer.INPUT_ERROR, f"{title}, {f.name}: {err}") from None


def _cell_label(v: Any) -> str:
    from dawg_replay.edge_values import cell_text

    return cell_text(v).strip()


def step_inputs(app: App, project: Project, pr: ProjectRun, node_id: str, edges: list[Edge]) -> Inputs:
    """Значения входов узла из активных стрелок: файлы, выходные значения, колонки, строки прохода.
    Ошибка: ReplayError input_error, шаг не стартует."""
    from dawg_replay.edge_values import coerce, column_values

    node = project.node(node_id)
    manifest = app.registry.manifest(node.script)
    fills = step_fills(app, project, pr, node_id, edges)
    problems: list[str] = []
    tables: dict[tuple[int, str], list[dict[str, Any]]] = {}
    each: list[tuple[Edge, Any, Any, list[Any]]] = []
    for e in edges:
        title = node_title(app, project, e.source)
        src = pr.step(e.source)
        rec = _run_of(app, pr, src) if src else None
        for m in e.maps:
            if m.kind == "file":
                continue
            try:
                p = manifest.param(m.param)
            except KeyError:
                problems.append(f"у скрипта {node.script} нет входа `{m.param}`")
                continue
            if rec is None:
                problems.append(f"{title}: нет запуска, значение нужно входу «{p.title}»")
                continue
            entry: dict[str, Any] = {"param": p.name, "source": rec.script_name, "run": rec.id, "node": e.source}
            if m.kind == "value":
                vals = _read_values(app, rec.id)
                if m.value not in vals:
                    continue  # не отдано: вход пуст, дальше обычное правило
                v, why = coerce(p, vals[m.value])
                if why:
                    problems.append(f"{title}, значение `{m.value}`: {why}")
                    continue
                fills.append(transfer.Fill(p.name, v, [], {**entry, "value": m.value}))
                continue
            key = (id(e), m.file)
            try:
                if key not in tables:
                    tables[key] = _source_table(app, project, rec, e, m.file)
                col = column_values(tables[key], m.column)
            except ReplayError as err:
                problems.append(err.message)
                continue
            except ValueError as err:
                problems.append(f"{title}, {m.file}: {err}")
                continue
            entry = {**entry, "file": m.file, "column": m.column, "rows": m.rows}
            if m.rows == "each":
                each.append((e, m, p, col))
                continue
            if m.rows == "first" and not col:
                continue  # строк нет: вход пуст
            v, why = coerce(p, col if m.rows == "all" else col[0])
            if why:
                problems.append(f"{title}, колонка «{m.column}»: {why}")
                continue
            fills.append(transfer.Fill(p.name, v, [], entry))
    if problems:
        text = problems[0] if len(problems) == 1 else "значения по стрелкам не подошли:\n" + "\n".join(f"  {x}" for x in problems)
        raise ReplayError(transfer.INPUT_ERROR, text, problems)
    fed = {m.param for e in project.incoming(node_id) for m in e.maps}
    rows: list[RowInput] | None = None
    if each:
        rows = []
        for i in range(min(len(col) for *_, col in each)):
            row_fills: list[transfer.Fill] = []
            shown: dict[str, Any] = {}
            cells: list[str] = []
            problem = None
            for e, m, p, col in each:
                cells.append(_cell_label(col[i]))
                v, why = coerce(p, col[i])
                if why:
                    problem = problem or why
                    continue
                if not p.is_secret:
                    shown[p.name] = v
                row_fills.append(transfer.Fill(p.name, v, [], {"param": p.name, "node": e.source, "file": m.file,
                                                               "column": m.column, "rows": "each", "row": i}))
            label = ", ".join(c for (_, _, p, _), c in zip(each, cells) if not p.is_secret) or f"строка {i + 1}"
            rows.append(RowInput(i, json.dumps(cells, ensure_ascii=False), label, row_fills, shown, problem))
    given = {f.param for f in fills} | {m.param for _, m, _, _ in each}
    return Inputs(fills, sorted(fed - given), rows, each[0][0] if each else None,
                  [m.column for _, m, _, _ in each])


# --- старт, остановка, перезапуск ------------------------------------------------------


def _deadline(project: Project) -> str | None:
    return _iso(_now() + timedelta(minutes=project.timeout_minutes)) if project.timeout_minutes else None


def start(app: App, name: str, *, schedule_id: str | None = None, notify: str | None = None,
          retry_of: str | None = None) -> ProjectRun:
    """Новый запуск проекта. Ручной при идущем запуске: ReplayError project_busy. Плановый при идущем:
    запись «пропущен» (failed, busy) и уведомление failed, ReplayError project_busy тоже."""
    from dawg_replay import launch, tg_project

    launch.recover_lost(app)
    base = get(app, retry_of) if retry_of else None
    stored = app.projects.load(name, base.version if base else None)
    project = stored.project
    from dawg_replay.projects import run_blockers

    errors = [x.text for x in run_blockers(app.projects.check(project))]
    if schedule_id is not None:
        _check_schedule(app, schedule_id, name)
    prun_id = new_id()
    trigger = "schedule" if schedule_id else None
    app.db.execute("BEGIN IMMEDIATE")
    try:
        busy = app.db.execute("SELECT id FROM project_runs WHERE project = ? AND status = ?", (name, RUNNING)).fetchone()
        status, reason, error = RUNNING, None, None
        if busy:
            status, reason, error = FAILED, REASON_BUSY, f"предыдущий запуск {busy[0]} ещё идёт"
        elif errors:
            status, reason, error = FAILED, REASON_NOT_STARTED, "проект не прошёл проверку: " + "; ".join(errors)
        if status == RUNNING or schedule_id is not None:
            now = utcnow()
            app.db.execute(
                "INSERT INTO project_runs (id, project, version, status, reason, error, created_at, finished_at,"
                " triggered_by, schedule_id, retry_of, deadline, notify) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (prun_id, name, stored.version.version, status, reason, error, now, None if status == RUNNING else now,
                 trigger, schedule_id, retry_of, _deadline(project) if status == RUNNING else None, notify))
            for pos, nid in enumerate(project.order()):
                old = base.step(nid) if base else None
                if status == RUNNING and old is not None and old.status == OK and old.run_id:
                    st, run_id, why, reused = OK, old.run_id, "готово в прошлом запуске", 1
                else:
                    st, run_id, why, reused = PENDING if status == RUNNING else SKIPPED, None, None, 0
                app.db.execute("INSERT INTO project_steps (project_run, node, pos, status, run_id, reason, reused,"
                               " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (prun_id, nid, pos, st, run_id, why, reused, now))
        app.db.execute("COMMIT")
    except BaseException:
        app.db.execute("ROLLBACK")
        raise
    recorded = prun_id if schedule_id is not None else None  # плановый отказ остаётся в истории
    if (busy or errors) and recorded:
        tg_project.after_finish(app, recorded)
    if busy:
        raise ReplayError("project_busy", f"Проект уже запущен: {busy[0]}. Откройте идущий запуск или остановите его: "
                                          f"dawg-replay project stop {busy[0]}", {"running": busy[0], "run": recorded})
    if errors:
        raise ReplayError("project_invalid", "проект не прошёл проверку:\n" + "\n".join(f"  {x}" for x in errors),
                          {"problems": errors, "run": recorded})
    _spawn_driver(app, prun_id)
    advance(app, prun_id)
    return get(app, prun_id)


def _check_schedule(app: App, schedule_id: str, name: str) -> None:
    row = app.db.execute("SELECT project FROM project_schedules WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise ReplayError("schedule_not_found", f"расписания `{schedule_id}` нет в приложении: задача планировщика "
                                                "осталась сиротой; снимите её: dawg-replay schedule sync")
    if row[0] != name:
        raise ReplayError("schedule_mismatch", f"расписание `{schedule_id}` относится к проекту `{row[0]}`, а не `{name}`")


def retry(app: App, prun_id: str) -> ProjectRun:
    """С упавшего: новый запуск по той же версии; «готово» берётся из прошлого запуска."""
    pr = get(app, prun_id)
    if pr.running:
        raise ReplayError("project_running", f"запуск {prun_id} ещё идёт: дождитесь его или остановите")
    return start(app, pr.project, retry_of=prun_id)


def stop(app: App, prun_id: str) -> ProjectRun:
    """Отменяет идущие шаги, остальные пропускает. Итог «отменён» ставит последний закрывшийся шаг."""
    pr = get(app, prun_id)
    if not pr.running:
        raise ReplayError("not_running", f"запуск проекта `{prun_id}` уже завершён: {pr.label}")
    app.db.execute("UPDATE project_runs SET cancel_requested_at = COALESCE(cancel_requested_at, ?) WHERE id = ?",
                   (utcnow(), prun_id))
    _cancel_steps(app, get(app, prun_id))
    advance(app, prun_id)
    return get(app, prun_id)


def _cancel_steps(app: App, pr: ProjectRun) -> None:
    from dawg_replay import launch

    for s in pr.steps:
        if s.status not in (STARTING, STEP_RUNNING):
            continue
        recs = [_row_run(app, pr.id, r) for r in s.rows if r.status in (STARTING, STEP_RUNNING)] if s.each \
            else [_run_of(app, pr, s)]
        for rec in recs:
            if rec is not None and (rec.running or rec.waiting):
                try:
                    launch.cancel(app, rec.id, wait=False)
                except ReplayError:
                    pass


# --- продвижение ------------------------------------------------------------------------


_local = threading.local()


def advance(app: App, prun_id: str) -> None:
    """Сверяет шаги с запусками, решает готовые узлы, запускает их, подводит итог. Можно звать
    сколько угодно раз и из разных процессов: каждое решение принимается один раз (кто первый)."""
    busy: dict[str, bool] = getattr(_local, "busy", None) or {}
    _local.busy = busy
    if prun_id in busy:
        busy[prun_id] = True  # вызвали изнутри (шаг закрылся в этом же потоке): пройти ещё раз
        return
    busy[prun_id] = False
    try:
        while True:
            busy[prun_id] = False
            _advance_once(app, prun_id)
            if not busy[prun_id]:
                break
    finally:
        busy.pop(prun_id, None)


def _advance_once(app: App, prun_id: str) -> None:
    pr = get(app, prun_id)
    if not pr.running:
        return
    project = _project(app, pr)
    _sync(app, pr)
    pr = get(app, prun_id)
    if pr.deadline and pr.reason != REASON_TIMEOUT and (parse_ts(pr.deadline) or _now()) <= _now():
        app.db.execute("UPDATE project_runs SET reason = ?, cancel_requested_at = COALESCE(cancel_requested_at, ?)"
                       " WHERE id = ? AND status = ?", (REASON_TIMEOUT, utcnow(), prun_id, RUNNING))
        _cancel_steps(app, pr)
        pr = get(app, prun_id)
    stopping = pr.cancel_requested_at is not None
    why_stop = "время запуска проекта вышло" if pr.reason == REASON_TIMEOUT else "запуск проекта остановлен"
    to_start: list[tuple[str, list[Edge]]] = []
    to_rows: list[tuple[str, int]] = []
    changed = True
    while changed:  # решения по одному узлу открывают следующие (пропуск тянет пропуск)
        changed = _drive_rows(app, project, pr, stopping, why_stop, to_rows)
        pr = get(app, prun_id)
        for s in pr.steps:
            if stopping and s.status == STARTING and _run_of(app, pr, s) is None:
                # Процесс шага ещё не создал запуск: шаг закрывается сразу, его запуск не начнётся.
                changed |= _set_step(app, prun_id, s.node, CANCELLED, only_from=(STARTING,), reason=why_stop)
                continue
            if s.status != PENDING:
                continue
            if stopping:
                changed |= _set_step(app, prun_id, s.node, SKIPPED, only_from=(PENDING,), reason=why_stop)
                continue
            d = decide(app, project, pr, s.node)
            if d is None:
                continue
            kind, why, edges = d
            if kind == "start":
                if _set_step(app, prun_id, s.node, STARTING, only_from=(PENDING,), reason=None):
                    to_start.append((s.node, edges))
                    changed = True
            else:
                changed |= _set_step(app, prun_id, s.node, kind, only_from=(PENDING,), reason=why)
        pr = get(app, prun_id)
    for node, _ in to_start:
        _spawn_step(app, prun_id, node)
    for node, idx in to_rows:
        _spawn_row(app, prun_id, node, idx)
    pr = get(app, prun_id)
    if pr.running and all(s.done for s in pr.steps):
        _conclude(app, pr)


def _sync_one(rec: Run | None, status: str, run_id: str | None, pid: int | None, pid_started: float | None,
              updated_at: str | None, setter: Callable[..., bool]) -> None:
    """Шаг или строка «запускается»/«идёт» по своему запуску: закончился, идёт, процесс пропал."""
    if rec is not None:
        if rec.running or rec.waiting:
            if status != STEP_RUNNING or run_id != rec.id:
                setter(STEP_RUNNING, only_from=(STARTING, STEP_RUNNING), run_id=rec.id)
            return
        st = _status_of_run(rec)
        why = _short(rec.error) if st in (NOT_STARTED, FAILED) else None
        setter(st, only_from=(STARTING, STEP_RUNNING), run_id=rec.id, reason=why)
        return
    # Запуска нет: процесс ещё готовит его, или пропал до этого.
    gone = pid is not None and not procinfo.same_process(pid, pid_started)
    late = pid is None and (parse_ts(updated_at) or _now()) < _now() - STARTING_GRACE
    if gone or late:
        setter(NOT_STARTED, only_from=(STARTING,), reason="процесс шага закрылся до запуска скрипта")


def _sync(app: App, pr: ProjectRun) -> None:
    """Шаги «запускается» и «идёт» по их запускам. Шаг прохода по строкам сверяется по строкам."""
    for s in pr.steps:
        if s.status not in (STARTING, STEP_RUNNING) or s.each:
            continue
        _sync_one(_run_of(app, pr, s), s.status, s.run_id, s.pid, s.pid_started, s.updated_at,
                  lambda st, _n=s.node, **kw: _set_step(app, pr.id, _n, st, **kw))


# --- проход по строкам --------------------------------------------------------------------


def each_edge(project: Project, node_id: str) -> Edge | None:
    """Стрелка, по таблице которой идёт проход узла, или None."""
    return next((e for e in project.incoming(node_id) if e.each), None)


def _drive_rows(app: App, project: Project, pr: ProjectRun, stopping: bool, why_stop: str,
                to_rows: list[tuple[str, int]]) -> bool:
    """Шаги прохода по строкам: сверить строки, остановить проход, запустить следующие, собрать итог.
    True, если какой-то шаг закрылся (это открывает решения по следующим узлам)."""
    changed = False
    for s in pr.steps:
        if s.status != STEP_RUNNING or not s.each:
            continue
        edge = each_edge(project, s.node)
        for r in s.rows:
            if r.status in (STARTING, STEP_RUNNING):
                _sync_one(_row_run(app, pr.id, r), r.status, r.run_id, r.pid, r.pid_started, r.updated_at,
                          lambda st, _r=r, **kw: _set_row(app, pr.id, _r.node, _r.index, st, **kw))
        rows = step_rows(app, pr.id, s.node)
        bad = next((r for r in rows if r.status in (FAILED, NOT_STARTED)), None)
        halt = why_stop if stopping else (
            f"проход остановлен: упала строка «{bad.label}»" if bad and edge is not None and edge.on_error == "stop"
            else None)
        if halt:
            for r in rows:
                if r.status == PENDING:
                    _set_row(app, pr.id, s.node, r.index, SKIPPED, only_from=(PENDING,), reason=halt)
                elif stopping and r.status == STARTING and _row_run(app, pr.id, r) is None:
                    _set_row(app, pr.id, s.node, r.index, CANCELLED, only_from=(STARTING,), reason=halt)
        else:
            limit = max(1, min(edge.parallel if edge else 1, app.config.max_parallel_runs))
            active = sum(1 for r in rows if r.status in (STARTING, STEP_RUNNING))
            for r in rows:
                if active >= limit:
                    break
                if r.status == PENDING and _set_row(app, pr.id, s.node, r.index, STARTING, only_from=(PENDING,)):
                    to_rows.append((s.node, r.index))
                    active += 1
        rows = step_rows(app, pr.id, s.node)
        if all(r.done for r in rows):
            changed |= _collect(app, project, get(app, pr.id), s.node, rows, edge)
    return changed


def _collect(app: App, project: Project, pr: ProjectRun, node_id: str, rows: list[StepRow], edge: Edge | None) -> bool:
    """Все строки решены: итог шага и запуск-сборка с файлами строк. Собирает тот, кто первым занял шаг."""
    from dawg_replay.runs import new_run_id

    step = pr.step(node_id)
    if step is None or step.status != STEP_RUNNING:
        return False
    good = [r for r in rows if r.status in (OK, PARTIAL)]
    failed = [r for r in rows if r.status in (FAILED, NOT_STARTED)]
    if pr.cancel_requested_at is not None:
        status = CANCELLED
    elif all(r.status == OK for r in rows):
        status = OK
    elif not good or (failed and edge is not None and edge.on_error == "stop"):
        status = FAILED
    else:
        status = PARTIAL
    text = rows_summary(rows, step.rows_total)["text"]
    merge_id = new_run_id() if good else None
    claimed = app.db.execute(
        "UPDATE project_steps SET run_id = ?, updated_at = ? WHERE project_run = ? AND node = ? AND status = ?"
        " AND run_id IS NULL", (merge_id or "", utcnow(), pr.id, node_id, STEP_RUNNING)).rowcount == 1
    if not claimed:
        return False
    if merge_id:
        try:
            _build_collected(app, project, pr, node_id, good, merge_id, status, f"строк готово {text}")
        except Exception as e:  # noqa: BLE001 сборка не должна оставить шаг висеть
            print(f"dawg-replay: сборка шага {node_id} не удалась: {e}", file=sys.stderr)
            merge_id = None
    _set_step(app, pr.id, node_id, status, only_from=(STEP_RUNNING,), run_id=merge_id,
              reason=None if status == OK else f"строк {text}")
    return True


TABLE_SUFFIXES = (".csv", ".tsv", ".xlsx", ".json")
_BAD_NAME = str.maketrans({c: "_" for c in '/\\:*?"<>|'})


def _write_table(path: Path, header: list[str], rows: list[dict[str, Any]]) -> None:
    import csv

    def cell(v: Any) -> Any:
        return ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else v)

    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(json.dumps([{k: r.get(k) for k in header} for r in rows], ensure_ascii=False, indent=1),
                        encoding="utf-8")
    elif suffix == ".xlsx":
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(header)
        for r in rows:
            ws.append([cell(r.get(k)) for k in header])
        wb.save(path)
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter="\t" if suffix == ".tsv" else ",")
            w.writerow(header)
            for r in rows:
                w.writerow([cell(r.get(k)) for k in header])


def _build_collected(app: App, project: Project, pr: ProjectRun, node_id: str, good: list[StepRow], merge_id: str,
                     status: str, summary: str) -> None:
    """Каталог результата сборки: одноимённые таблицы склеены с колонками прохода, прочие файлы
    рядом с подписью строки, выходные значения строк в values.csv. Затем запись запуска."""
    import shutil

    from dawg_replay_kit.tables import TableError, parse_table

    from dawg_replay.runner import last_result

    node = project.node(node_id)
    edge = each_edge(project, node_id)
    columns = [m.column for m in edge.each] if edge else []
    out = transfer.output_dir(app, merge_id)
    out.mkdir(parents=True, exist_ok=True)
    by_name: dict[str, list[tuple[StepRow, Path]]] = {}
    mains: list[str] = []
    for r in good:
        root = transfer.output_dir(app, r.run_id or "")
        if root.is_dir():
            for f in sorted(root.rglob("*")):
                if f.is_file():
                    by_name.setdefault(f.relative_to(root).as_posix(), []).append((r, f))
        res = last_result(app.paths.run_dir(r.run_id or "") / "events.jsonl") or {}
        if isinstance(res.get("main"), str) and res["main"]:
            mains.append(res["main"])

    def row_cells(r: StepRow) -> dict[str, Any]:
        return dict(zip(columns, json.loads(r.key)))

    for rel, items in by_name.items():
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() in TABLE_SUFFIXES:
            try:
                merged: list[dict[str, Any]] = []
                header: list[str] = []
                for r, f in items:
                    extra = row_cells(r)
                    for row in parse_table(f.read_bytes(), f.name):
                        clean = {k: v for k, v in row.items() if not k.startswith("_")}
                        merged.append({**{c: v for c, v in extra.items() if c not in clean}, **clean})
                for row in merged:
                    header += [k for k in row if k not in header]
                if merged:
                    _write_table(dest, header, merged)
                    continue
            except (TableError, OSError, ValueError):
                pass
        if len(items) == 1:
            shutil.copyfile(items[0][1], dest)
            continue
        for r, f in items:
            shutil.copyfile(f, dest.with_name(f"{dest.stem} ({r.label.translate(_BAD_NAME)}){dest.suffix}"))

    if "values.csv" not in by_name:
        table: list[dict[str, Any]] = []
        for r in good:
            vals = {v["name"]: v["value"] for v in _shown_values(app, r.run_id or "") if not v.get("secret")}
            if vals:
                table.append({**row_cells(r), **vals})
        if table:
            header = []
            for row in table:
                header += [k for k in row if k not in header]
            _write_table(out / "values.csv", header, table)

    run_status = {OK: "ok", PARTIAL: "partial"}.get(status, "failed")
    event: dict[str, Any] = {"t": "result", "status": run_status, "summary": summary}
    if mains and len(set(mains)) == 1 and (out / mains[0]).is_file():
        event["main"] = mains[0]
    (app.paths.run_dir(merge_id) / "events.jsonl").write_text(json.dumps(event, ensure_ascii=False) + "\n",
                                                               encoding="utf-8")
    try:
        version = app.registry.version(node.script)
        ver, path = version.version, str(version.path)
    except ReplayError:
        ver, path = None, ""
    app.runs.record_collected(run_id=merge_id, script_name=node.script, script_version=ver, script_path=path,
                              preset=node.preset, status=run_status, summary=summary, created_at=utcnow(),
                              project_run=pr.id, project_node=node_id)


def _conclude(app: App, pr: ProjectRun) -> None:
    from dawg_replay import tg_project

    statuses = [s.status for s in pr.steps]
    reason = None
    if pr.reason == REASON_TIMEOUT:
        status, reason = FAILED, REASON_TIMEOUT
    elif pr.cancel_requested_at is not None:
        status = CANCELLED
    elif any(x in (FAILED, NOT_STARTED, CANCELLED) for x in statuses):
        status = FAILED
    elif any(x in (PARTIAL, SKIPPED) for x in statuses):
        status = PARTIAL
    else:
        status = OK
    if _finish(app, pr.id, status, reason):
        tg_project.after_finish(app, pr.id)


def on_step_finish(app: App, run_id: str) -> None:
    """Запуск закончился: если это шаг проекта, решить следующие узлы. Ошибки не доходят до запуска."""
    try:
        rec = app.runs.get(run_id)
        if rec.project_run:
            advance(app, rec.project_run)
    except Exception as e:  # noqa: BLE001 решение по проекту не должно ломать закрытие запуска
        print(f"dawg-replay: запуск проекта не продвинулся: {e}", file=sys.stderr)


def recover(app: App) -> None:
    """Запуски проектов без живого ведущего: решить за него (срок, закрывшиеся шаги) и поднять нового."""
    for pr in running(app):
        if procinfo.same_process(pr.driver_pid, pr.driver_started):
            continue
        try:
            advance(app, pr.id)
        except ReplayError:
            continue
        if get(app, pr.id).running:
            _spawn_driver(app, pr.id)
    from dawg_replay import tg_project

    tg_project.retry_pending(app)


# --- шаг ----------------------------------------------------------------------------------


def run_step(app: App, prun_id: str, node_id: str) -> Run | None:
    """Шаг в этом процессе: значения стрелок, ожидание места, запуск и ведение скрипта.
    Не начался (параметры, файлы, нет места): запись в истории скрипта и шаг «не стартовал».
    У прохода по строкам шаг только выбирает строки: их запускает `advance`, каждую своим процессом."""
    pr = get(app, prun_id)
    step = pr.step(node_id)
    if not pr.running or step is None or step.status != STARTING:
        return None
    pid = os.getpid()
    app.db.execute("UPDATE project_steps SET pid = ?, pid_started = ? WHERE project_run = ? AND node = ?",
                   (pid, procinfo.start_time(pid), prun_id, node_id))

    def setter(status: str, **kw: Any) -> bool:
        return _set_step(app, prun_id, node_id, status, **kw)

    def build(inputs: Inputs) -> tuple[list[transfer.Fill], list[str]] | None:
        if inputs.rows is None:
            return inputs.fills, inputs.unset
        _begin_rows(app, pr, node_id, inputs.rows)
        return None

    return _launch_node(app, pr, node_id, None, setter, build)


def run_row(app: App, prun_id: str, node_id: str, idx: int) -> Run | None:
    """Строка прохода в этом процессе: значения строки, ожидание места, запуск и ведение скрипта."""
    pr = get(app, prun_id)
    step = pr.step(node_id)
    row = next((r for r in step.rows if r.index == idx), None) if step else None
    if not pr.running or row is None or row.status != STARTING:
        return None
    pid = os.getpid()
    app.db.execute("UPDATE project_step_rows SET pid = ?, pid_started = ? WHERE project_run = ? AND node = ? AND idx = ?",
                   (pid, procinfo.start_time(pid), prun_id, node_id, idx))

    def setter(status: str, **kw: Any) -> bool:
        return _set_row(app, prun_id, node_id, idx, status, **kw)

    def build(inputs: Inputs) -> tuple[list[transfer.Fill], list[str]] | None:
        ri = inputs.rows[idx] if inputs.rows is not None and idx < len(inputs.rows) else None
        if ri is None or ri.key != row.key:
            raise ReplayError(transfer.INPUT_ERROR, "таблица строк изменилась с начала шага")
        if ri.problem:
            raise ReplayError(transfer.INPUT_ERROR, ri.problem)
        return [*inputs.fills, *ri.fills], inputs.unset

    return _launch_node(app, pr, node_id, idx, setter, build)


def _launch_node(app: App, pr: ProjectRun, node_id: str, idx: int | None, setter: Callable[..., bool],
                 build: Callable[[Inputs], tuple[list[transfer.Fill], list[str]] | None]) -> Run | None:
    from dawg_replay import launch

    prun_id = pr.id
    project = _project(app, pr)
    node = project.node(node_id)
    try:
        d = decide(app, project, pr, node_id)
        edges = d[2] if d and d[0] == "start" else []
        got = build(step_inputs(app, project, pr, node_id, edges))
        if got is None:
            return None
        fills, unset = got
        # Вход, в который идёт стрелка, без значения по ней пуст: пресет и значения узла его не заполняют.
        fed = {m.param for e in project.incoming(node_id) for m in e.maps}
        values = {k: v for k, v in node.values.items() if k not in fed}
        prepared = launch.prepare(app, node.script, preset=node.preset, values=values, given=fills, unset=unset,
                                  wait=True, strict_preset=True, project_run=prun_id, project_node=node_id,
                                  project_row=idx)
    except ReplayError as e:
        if e.code == "run_skipped":
            setter(NOT_STARTED, only_from=(STARTING, STEP_RUNNING), run_id=e.details.get("run"),
                   reason="не дождался места для запуска: " + e.message)
        elif e.code == "run_cancelled":
            setter(CANCELLED, only_from=(STARTING, STEP_RUNNING), run_id=e.details.get("run"), reason="отменён в очереди")
        else:
            rec = launch.record_not_started(app, None, node.script, node.preset, e, project_run=prun_id,
                                            project_node=node_id, project_row=idx)
            setter(NOT_STARTED, only_from=(STARTING,), run_id=rec.id if rec else None, reason=_short(e.message))
        advance(app, prun_id)
        return None
    if not setter(STEP_RUNNING, only_from=(STARTING,), run_id=prepared.run.id):
        # Пока готовился запуск, проект остановили или вышел срок: скрипт не начинается.
        app.runs.finish(prepared.run.id, status="failed", reason=REASON_CANCELLED, error="запуск проекта остановлен")
        setter(CANCELLED, only_from=(CANCELLED,), run_id=prepared.run.id)
        advance(app, prun_id)
        return app.runs.get(prepared.run.id)
    return launch.supervise(app, prepared.run.id, prepared.payload())


def _begin_rows(app: App, pr: ProjectRun, node_id: str, rows: list[RowInput]) -> None:
    """Строки выбраны: шаг «идёт», строки ждут очереди. Перезапуск берёт «готово» строк прошлого запуска."""
    if not rows:
        _set_step(app, pr.id, node_id, SKIPPED, only_from=(STARTING,), reason="нет строк", rows_total=0)
        advance(app, pr.id)
        return
    reuse: dict[str, StepRow] = {}
    if pr.retry_of:
        for r in step_rows(app, pr.retry_of, node_id):
            if r.status == OK and r.run_id:
                reuse.setdefault(r.key, r)
    now = utcnow()
    app.db.execute("BEGIN IMMEDIATE")
    try:
        cur = app.db.execute("UPDATE project_steps SET status = ?, rows_total = ?, updated_at = ? WHERE project_run = ?"
                             " AND node = ? AND status = ?", (STEP_RUNNING, len(rows), now, pr.id, node_id, STARTING))
        if cur.rowcount == 1:
            for r in rows:
                old = reuse.pop(r.key, None)
                if old is not None:
                    st, run_id, why, reused = OK, old.run_id, "готово в прошлом запуске", 1
                elif r.problem:
                    st, run_id, why, reused = NOT_STARTED, None, _short(r.problem), 0
                else:
                    st, run_id, why, reused = PENDING, None, None, 0
                app.db.execute(
                    "INSERT INTO project_step_rows (project_run, node, idx, status, key, label, values_json, run_id,"
                    " reason, reused, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pr.id, node_id, r.index, st, r.key, r.label, json.dumps(r.shown, ensure_ascii=False) if r.shown
                     else None, run_id, why, reused, now))
        app.db.execute("COMMIT")
    except BaseException:
        app.db.execute("ROLLBACK")
        raise
    advance(app, pr.id)


def step_label(app: App, rec: Run) -> str | None:
    """«Проект: узел» для шага проекта (вопросы в Telegram, экран запуска), иначе None."""
    if not rec.project_run or not rec.project_node:
        return None
    try:
        pr = get(app, rec.project_run)
        stored = app.projects.load(pr.project, pr.version)
    except ReplayError:
        return None
    return f"{stored.project.title}: {node_title(app, stored.project, rec.project_node)}"


# --- процессы --------------------------------------------------------------------------


def _processes_on() -> bool:
    return os.environ.get(ENV_PROCESSES, "").strip().lower() != "off"


def _detached_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def log_dir(app: App) -> Path:
    return app.paths.home / "project-runs"


def _spawn(app: App, args: list[str]) -> subprocess.Popen | None:
    d = log_dir(app)
    d.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "dawg_replay.project_runs", "--home", str(app.paths.home), *args]
    try:
        with open(d / f"{args[1]}.log", "ab") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **_detached_kwargs())
    except OSError as e:
        print(f"dawg-replay: процесс запуска проекта не поднялся: {e}", file=sys.stderr)
        return None
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _spawn_step_process(app: App, prun_id: str, node_id: str) -> None:
    if not _processes_on():
        return
    proc = _spawn(app, ["step", prun_id, node_id])
    if proc is not None:
        app.db.execute("UPDATE project_steps SET pid = ?, pid_started = ? WHERE project_run = ? AND node = ?"
                       " AND pid IS NULL", (proc.pid, procinfo.start_time(proc.pid), prun_id, node_id))
    else:
        _set_step(app, prun_id, node_id, NOT_STARTED, only_from=(STARTING,), reason="процесс шага не запустился")


_spawn_step: Callable[[App, str, str], None] = _spawn_step_process


def _spawn_row_process(app: App, prun_id: str, node_id: str, idx: int) -> None:
    if not _processes_on():
        return
    proc = _spawn(app, ["row", prun_id, node_id, str(idx)])
    if proc is not None:
        app.db.execute("UPDATE project_step_rows SET pid = ?, pid_started = ? WHERE project_run = ? AND node = ?"
                       " AND idx = ? AND pid IS NULL", (proc.pid, procinfo.start_time(proc.pid), prun_id, node_id, idx))
    else:
        _set_row(app, prun_id, node_id, idx, NOT_STARTED, only_from=(STARTING,), reason="процесс строки не запустился")


_spawn_row: Callable[[App, str, str, int], None] = _spawn_row_process


def _spawn_driver(app: App, prun_id: str) -> None:
    if not _processes_on():
        return
    proc = _spawn(app, ["drive", prun_id])
    if proc is not None:
        app.db.execute("UPDATE project_runs SET driver_pid = ?, driver_started = ?, heartbeat_at = ? WHERE id = ?",
                       (proc.pid, procinfo.start_time(proc.pid), utcnow(), prun_id))


def drive(app: App, prun_id: str, poll: float = POLL_SECONDS, sleep: Callable[[float], None] = time.sleep) -> None:
    """Ведущий: пока запуск проекта идёт, раз в `poll` секунд решает и следит за сроком."""
    pid = os.getpid()
    app.db.execute("UPDATE project_runs SET driver_pid = ?, driver_started = ? WHERE id = ?",
                   (pid, procinfo.start_time(pid), prun_id))
    while True:
        try:
            pr = get(app, prun_id)
        except ReplayError:
            return
        if not pr.running or pr.driver_pid != pid:
            return  # закончился или уже ведёт другой
        app.db.execute("UPDATE project_runs SET heartbeat_at = ? WHERE id = ?", (utcnow(), prun_id))
        advance(app, prun_id)
        sleep(poll)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dawg_replay.project_runs")
    parser.add_argument("--home", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drive")
    d.add_argument("id")
    s = sub.add_parser("step")
    s.add_argument("id")
    s.add_argument("node")
    r = sub.add_parser("row")
    r.add_argument("id")
    r.add_argument("node")
    r.add_argument("idx", type=int)
    args = parser.parse_args(argv)
    from dawg_replay.app import App

    with App(Path(args.home)) as app:
        if args.cmd == "drive":
            drive(app, args.id)
        elif args.cmd == "row":
            run_row(app, args.id, args.node, args.idx)
        else:
            run_step(app, args.id, args.node)
    return 0


if __name__ == "__main__":
    sys.exit(main())
