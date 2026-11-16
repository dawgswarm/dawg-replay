"""Итог запуска проекта одним сообщением в Telegram (шаги своих сообщений об итоге не шлют).

    Готово частично: Карточки WB в Google
    5 из 6, пропущен: Google
    пропущен: Google, WB: магазин 2 закончился частично, а стрелка ждёт: готово
    Шёл 12 мин, начат сегодня 09:00

Правила как у скрипта (`failed`, `done_scheduled`, `done_manual` с `long_minutes`, `suspicious`),
уровни: общие настройки, проект, расписание проекта, запуск (`notify`). Отмену человек сделал сам:
о ней не пишется. Без дублей: запуск проекта захватывается в БД (`notify_state`), как запуск скрипта.
Неотправленное пробует ещё раз `retry_pending` (зовёт `project_runs.recover`), в течение суток.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from dawg_replay import diagnosis, repair, tg_notify, tg_rules
from dawg_replay.errors import ReplayError
from dawg_replay.runs import parse_ts

if TYPE_CHECKING:
    from dawg_replay.app import App
    from dawg_replay.project_runs import ProjectRun

SENDING, SENT, FAILED, NONE = tg_notify.SENDING, tg_notify.SENT, tg_notify.FAILED, tg_notify.NONE
MAX_AGE = tg_notify.MAX_AGE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def duration_seconds(pr: ProjectRun) -> float | None:
    start, end = parse_ts(pr.created_at), parse_ts(pr.finished_at)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def suspicions(app: App, pr: ProjectRun) -> list[str]:
    """Тихие поломки шагов: «узел: текст»."""
    from dawg_replay import project_runs as P

    out = []
    project = None
    for s in pr.steps:
        if s.status not in (P.OK, P.PARTIAL) or not s.run_id or s.reused:
            continue
        try:
            rec = app.runs.get(s.run_id)
            sus = repair.suspicion(app, rec)
        except (ReplayError, OSError):
            continue
        if sus:
            if project is None:
                project = app.projects.load(pr.project, pr.version).project
            out.append(f"{P.node_title(app, project, s.node)}: {sus['text']}")
    return out


def plan(app: App, pr: ProjectRun, sus: list[str]) -> str | None:
    """failed, done или None (ничего)."""
    from dawg_replay import project_runs as P

    if pr.status not in (P.OK, P.PARTIAL, P.FAILED):
        return None  # идёт или отменён человеком
    rules = tg_rules.effective_project(app, pr.project, pr.schedule_id)
    if pr.status == P.FAILED:
        return "failed" if rules["failed"] else None
    if pr.notify == "never":
        return None
    if pr.notify == "always":
        done = True
    elif pr.trigger == "schedule":
        done = bool(rules["done_scheduled"])
    elif rules["done_manual"] == "always":
        done = True
    elif rules["done_manual"] == "long":
        took = duration_seconds(pr)
        done = took is not None and took >= rules["long_minutes"] * 60
    else:
        done = False
    if done or (sus and rules["suspicious"]):
        return "done"
    return None


def compose(app: App, pr: ProjectRun, kind: str, sus: list[str]) -> str:
    from dawg_replay import project_runs as P

    try:
        project = app.projects.load(pr.project, pr.version).project
        title = project.title or pr.project
    except ReplayError:
        project, title = None, pr.project

    def name(node: str) -> str:
        return P.node_title(app, project, node) if project is not None else node

    lines = []
    if kind == "failed":
        lines.append(f"Упал: {title}")
    else:
        lines.append(f"{'Готово частично' if pr.status == P.PARTIAL else 'Готово'}: {title}")
    if pr.reason == P.REASON_BUSY:
        lines.append("Плановый запуск пропущен: предыдущий запуск проекта ещё идёт.")
    elif pr.reason == P.REASON_NOT_STARTED and pr.error:
        lines.append(f"Не начался: {pr.error}")
    else:
        counted = [s for s in pr.steps if s.status != P.OFF_BRANCH]
        good = sum(1 for s in counted if s.status in (P.OK, P.PARTIAL))
        parts = [f"{good} из {len(counted)}"]
        for status in (P.FAILED, P.NOT_STARTED, P.CANCELLED, P.SKIPPED, P.PARTIAL):
            names = [name(s.node) for s in counted if s.status == status]
            if names:
                parts.append(f"{P.STEP_TITLES[status]}: {', '.join(names)}")
        lines.append(", ".join(parts))
        if pr.reason == P.REASON_TIMEOUT:
            lines.append("Время запуска проекта вышло: идущие шаги остановлены.")
        for s in pr.steps:
            if s.status in (P.FAILED, P.NOT_STARTED):
                why = _step_reason(app, s)
                lines.append(f"{P.STEP_TITLES[s.status]}: {name(s.node)}" + (f", {why}" if why else ""))
        for s in pr.steps:
            if s.status == P.SKIPPED and s.reason:
                lines.append(f"пропущен: {name(s.node)}, {s.reason}")
        for text in sus:
            lines.append(f"Похоже, сайт поменялся. {text}")
    took = duration_seconds(pr)
    when = tg_notify.moment_text(parse_ts(pr.created_at))
    if took is not None and pr.reason != P.REASON_BUSY:
        lines.append(f"Шёл {tg_notify.duration_text(took)}, начат {when}")
    elif when:
        lines.append(f"Время: {when}")
    lines.append(f"В приложении: Проекты, {title}, {when}")
    hide: list[str] = []
    for script in {project.node(s.node).script for s in pr.steps} if project is not None else ():
        hide += diagnosis.known_secrets(app, script)
    return diagnosis.scrub("\n".join(lines), hide)


def _step_reason(app: App, step: Any) -> str:
    if step.run_id:
        try:
            rec = app.runs.get(step.run_id)
            scan = repair.scan(app.paths.run_dir(rec.id)) if app.paths.run_dir(rec.id).is_dir() else None
            return tg_notify.failure_reason(app, rec, scan).rstrip(".")
        except (ReplayError, OSError):
            pass
    return (step.reason or "").rstrip(".")


def _claim(app: App, prun_id: str) -> bool:
    cur = app.db.execute(
        "UPDATE project_runs SET notify_state = ? WHERE id = ? AND status IN ('ok', 'partial', 'failed')"
        " AND (notify_state IS NULL OR notify_state = ?)", (SENDING, prun_id, FAILED))
    return cur.rowcount == 1


def _mark(app: App, prun_id: str, state: str) -> None:
    app.db.execute("UPDATE project_runs SET notify_state = ? WHERE id = ?", (state, prun_id))


def after_finish(app: App, prun_id: str) -> bool | None:
    """Отправляет итог закрытого запуска проекта, если нужно. True отправлено, False не вышло,
    None: слать нечего или уже отправлено. Никогда не бросает."""
    from dawg_replay import project_runs as P

    try:
        if not _claim(app, prun_id):
            return None
        pr = P.get(app, prun_id)
        finished = parse_ts(pr.finished_at)
        conn = tg_notify._connection(app)
        sus = suspicions(app, pr) if pr.status in (P.OK, P.PARTIAL) else []
        kind = plan(app, pr, sus) if conn is not None and (finished is None or _now() - finished <= MAX_AGE) else None
        if kind is None:
            _mark(app, prun_id, NONE)
            return None
        out = tg_notify.Outgoing(prun_id, P.log_dir(app) / prun_id, conn,
                                 tg_notify.Message(compose(app, pr, kind, sus), None), app.paths.db)
        ok = False
        try:
            ok = tg_notify._deliver(app, out, 1)
        except Exception as e:  # noqa: BLE001
            out.errors.append(str(e))
        _mark(app, prun_id, SENT if ok else FAILED)
        if not ok and out.errors:
            tg_notify._record_error(app, out.errors[-1])
        return ok
    except Exception:  # noqa: BLE001 Telegram не должен мешать запуску проекта
        return None


def retry_pending(app: App) -> list[str]:
    since = _iso(_now() - MAX_AGE)
    rows = app.db.execute(
        "SELECT id FROM project_runs WHERE status IN ('ok', 'partial', 'failed') AND finished_at >= ?"
        " AND notify_state = ? ORDER BY finished_at LIMIT 5", (since, FAILED)).fetchall()
    sent = []
    for row in rows:
        res = after_finish(app, row[0])
        if res is True:
            sent.append(row[0])
        elif res is False:
            break
    return sent
