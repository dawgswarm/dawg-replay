"""Автозапуск по связям (M6): решение, запускать ли получателя, когда закончился запуск источника.

Своего демона нет. Решает процесс, который закрыл запуск: `launch.supervise` после итога и сверки
выходов, `recover_lost` для потерянных, отмена без супервизора. Он зовёт `on_finish(app, run_id)`.

- Исход источника подходит связи по её условию (links.suitable). Не стартовавший запуск
  (not_started, skipped, start_error) и отмена человеком поводом не являются.
- Режим получателя `any`: запуск после каждого подходящего исхода источника.
- Режим `all` (по умолчанию): круг ожидания. Первый исход любого источника с запуском открывает
  круг со сроком; получатель запускается один раз, когда у всех источников есть подходящий исход.
  По сроку: `skip` пишет пропуск в историю получателя (failed, skipped) и шлёт failed в Telegram,
  `run` запускает с тем, что есть (по остальным связям берётся последний подходящий результат).
- Предел `limit_per_day` считается по журналу связи за календарные сутки по местному времени.
- Пока получатель идёт по связи, новый повод откладывается (один), следующие сливаются с ним.
- Запуск идёт как плановый: тот же лимит параллельных запусков, ожидание слота, процесс-супервизор.

Срок ожидания без открытого UI: `ensure_waiter` поднимает отдельный процесс
(`python -m dawg_replay.chain --home <dir> wait`), он раз в минуту проверяет сроки (`tick`)
и выходит, когда открытых кругов нет. Если компьютер был выключен, срок обработает первый
следующий `tick`: его зовут каждое завершение запуска, `recover_lost` (любой запуск, команды
CLI, старт UI) и этот процесс. Опоздание больше LATE_SECONDS видно в журнале.

Тесты подменяют `_spawn` (запуск получателя) и выключают процесс ожидания переменной
DAWG_REPLAY_CHAIN_WAITER=off.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay import links as L
from dawg_replay import procinfo
from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.runs import REASON_SKIPPED, Run, parse_ts

if TYPE_CHECKING:
    from dawg_replay.app import App
    from dawg_replay.launch import Prepared

ENV_WAITER = "DAWG_REPLAY_CHAIN_WAITER"  # off: не поднимать процесс ожидания сроков (тесты)
WAITER_FILE = "chain-waiter.json"
WAITER_POLL_SECONDS = 60.0
LATE_SECONDS = 5 * 60  # срок обработан позже этого: компьютер был выключен или спал
LOG_SOURCE = "links"

# Решения в журнале связи (link_log.decision)
STARTED = "started"
WAITING = "waiting"
UNSUITABLE = "unsuitable"
DISABLED = "disabled"
LIMIT = "limit"
DEFERRED = "deferred"
MERGED = "merged"
QUEUED = "queued"
NOT_STARTED = "not_started"
SKIPPED = "skipped"
ERROR = "error"

STATUS_WORDS = {"ok": "закончился успешно", "partial": "закончился частично", "failed": "упал"}
NEED_WORDS = {"ok": "нужен успешный", "partial": "нужен успешный или частичный", "any": "нужен любой"}

_local = threading.local()


# --- мелочи ---------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def moment(ts: str | None) -> str:
    """Местное время для журнала: `03:12` сегодня, иначе `15.11 03:12`."""
    dt = parse_ts(ts)
    if dt is None:
        return ""
    local = dt.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def _title(app: App, script: str) -> str:
    try:
        return app.registry.get(script).title or script
    except ReplayError:
        return script


def _run_log(app: App, run_id: str | None, msg: str, level: str = "info") -> None:
    """Строка в журнал запуска (events.jsonl), как пишут страховка и Telegram."""
    if not run_id:
        return
    run_dir = app.paths.run_dir(run_id)
    if not run_dir.is_dir():
        return
    e = {"t": "log", "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
         "level": level, "msg": msg, "source": LOG_SOURCE}
    try:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _outcome_text(app: App, rec: Run) -> str:
    word = STATUS_WORDS.get(rec.status, rec.status)
    if rec.status == "failed" and rec.reason == "timeout":
        word = "остановлен по времени"
    elif rec.status == "failed" and rec.reason == "lost":
        word = "прервался"
    return f"{_title(app, rec.script_name)} {word}"


def _not_passed(app: App, link: L.Link, rec: Run, why: str) -> None:
    """«Не передал»: строка в журнале запуска источника, если связь передаёт файлы."""
    if link.files:
        _run_log(app, rec.id, f"Не передал в «{_title(app, link.target)}»: {why}", "warn")


# --- точка входа -------------------------------------------------------------------


def on_finish(app: App, run_id: str) -> None:
    """Запуск закончился: отложенный повод его скрипта, решения по исходящим связям, сроки кругов.
    Ошибки здесь не ломают закрытие запуска: строка в журнале запуска."""
    if getattr(_local, "busy", False):
        return
    _local.busy = True
    try:
        tick(app, nested=True)
        rec = app.runs.get(run_id)
        if rec.draft or rec.running or rec.waiting:
            return
        if rec.trigger == "link":
            start_pending(app, rec.script_name)
        for link in app.links.outgoing(rec.script_name):
            if link.starts:
                decide(app, link, rec)
    except Exception as e:  # noqa: BLE001 - запуск уже закрыт, автозапуск не должен его ломать
        _run_log(app, run_id, f"Связи: решение о запуске получателей не принято: {type(e).__name__}: {e}", "error")
    finally:
        _local.busy = False


def decide(app: App, link: L.Link, rec: Run) -> None:
    """Одна связь с запуском и один закончившийся запуск её источника."""
    if rec.reason in L.NOT_REAL:
        return  # не шёл или отменён человеком: не повод
    if not link.enabled:
        app.links.log(link.id, DISABLED, f"не запущен: связь выключена ({_outcome_text(app, rec)})", rec.id)
        _not_passed(app, link, rec, "связь выключена")
        return
    ok = L.suitable(link.cond, rec.status, rec.reason)
    mode = app.links.mode(link.target)
    if mode.mode == "any":
        if not ok:
            app.links.log(link.id, UNSUITABLE, f"не запущен: {_outcome_text(app, rec)}, {NEED_WORDS[link.cond]}", rec.id)
            _not_passed(app, link, rec, f"{STATUS_WORDS.get(rec.status, rec.status)}, {NEED_WORDS[link.cond]}")
            return
        trigger(app, link.target, [link], {link.id: rec.id}, link, rec.id)
        return
    starting = [x for x in app.links.incoming(link.target) if x.starts and x.enabled]
    complete, arrived, deadline = _arrive(app, link, rec, ok, mode.wait_seconds, starting)
    if not ok:
        app.links.log(link.id, UNSUITABLE,
                      f"не запущен: {_outcome_text(app, rec)}, {NEED_WORDS[link.cond]}; круг ждёт до {moment(deadline)}",
                      rec.id)
        _not_passed(app, link, rec, f"{STATUS_WORDS.get(rec.status, rec.status)}, {NEED_WORDS[link.cond]}")
        ensure_waiter(app)
        return
    if not complete:
        wait_for = ", ".join(_title(app, x.source) for x in starting if str(x.id) not in arrived)
        app.links.log(link.id, WAITING, f"ждёт остальных источников ({wait_for}) до {moment(deadline)}", rec.id)
        ensure_waiter(app)
        return
    sources = {int(k): v for k, v in arrived.items()}
    trigger(app, link.target, starting, sources, link, rec.id)


def _arrive(app: App, link: L.Link, rec: Run, ok: bool, wait_seconds: int,
            starting: list[L.Link]) -> tuple[bool, dict[str, str], str]:
    """Отмечает исход в круге «после всех». (круг собран и закрыт, подходящие исходы, срок)."""
    db = app.db
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM link_rounds WHERE target = ?", (link.target,)).fetchone()
        now = _now()
        if row is None:
            arrived: dict[str, str] = {}
            deadline = _iso(now + timedelta(seconds=wait_seconds))
            db.execute("INSERT INTO link_rounds (target, opened_at, deadline, arrived_json) VALUES (?, ?, ?, '{}')",
                       (link.target, _iso(now), deadline))
        else:
            arrived = json.loads(row["arrived_json"] or "{}")
            deadline = row["deadline"]
        if ok:
            arrived[str(link.id)] = rec.id
        complete = ok and bool(starting) and all(str(x.id) in arrived for x in starting)
        if complete:
            db.execute("DELETE FROM link_rounds WHERE target = ?", (link.target,))
        else:
            db.execute("UPDATE link_rounds SET arrived_json = ?, last_link = ?, last_run = ? WHERE target = ?",
                       (json.dumps(arrived), link.id, rec.id, link.target))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return complete, arrived, deadline


# --- запуск получателя -----------------------------------------------------------------


def _local_midnight_utc() -> str:
    local_now = datetime.now().astimezone()
    return _iso(local_now.replace(hour=0, minute=0, second=0, microsecond=0))


def started_today(app: App, link_id: int) -> int:
    row = app.db.execute("SELECT COUNT(*) FROM link_log WHERE link_id = ? AND decision = ? AND at >= ?",
                         (link_id, STARTED, _local_midnight_utc())).fetchone()
    return int(row[0])


def _busy(app: App, target: str) -> Run | None:
    """Запуск получателя по связи, который ещё идёт или ждёт слота."""
    row = app.db.execute(
        "SELECT id FROM runs WHERE script_name = ? AND triggered_by = 'link' AND status IN ('running', 'waiting')"
        " ORDER BY created_at DESC LIMIT 1", (target,)).fetchone()
    return app.runs.get(row["id"]) if row else None


def trigger(app: App, target: str, links: list[L.Link], sources: dict[int, str], link: L.Link,
            source_run: str | None, note: str | None = None) -> Run | None:
    """Повод запустить получателя: предел, отложенный повод, потом запуск. Возвращает запуск или None."""
    for x in links:
        if x.limit_per_day is not None and started_today(app, x.id) >= x.limit_per_day:
            app.links.log(x.id, LIMIT, f"не запущен: исчерпан предел {x.limit_per_day} в сутки", source_run)
            src = _source(app, source_run)
            if src is not None and src.script_name == x.source:
                _not_passed(app, x, src, f"исчерпан предел {x.limit_per_day} запусков в сутки")
            return None
    running = _busy(app, target)
    if running is not None:
        _defer(app, target, links, sources, link, source_run, note, running)
        return None
    return launch_target(app, target, links, sources, link, source_run, note)


def _source(app: App, run_id: str | None) -> Run | None:
    if not run_id:
        return None
    try:
        return app.runs.get(run_id)
    except ReplayError:
        return None


def _defer(app: App, target: str, links: list[L.Link], sources: dict[int, str], link: L.Link,
           source_run: str | None, note: str | None, running: Run) -> None:
    """Получатель ещё идёт по связи: повод ждёт его конца. Отложенный один, новый повод заменяет его."""
    db = app.db
    db.execute("BEGIN IMMEDIATE")
    try:
        had = db.execute("SELECT 1 FROM link_pending WHERE target = ?", (target,)).fetchone() is not None
        db.execute(
            "INSERT INTO link_pending (target, link_id, source_run, sources_json, links_json, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (target) DO UPDATE SET link_id = excluded.link_id,"
            " source_run = excluded.source_run, sources_json = excluded.sources_json, links_json = excluded.links_json,"
            " note = excluded.note, created_at = excluded.created_at",
            (target, link.id, source_run, json.dumps({str(k): v for k, v in sources.items()}),
             json.dumps([x.id for x in links]), note, utcnow()),
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    if had:
        text = f"повод слит с уже отложенным: запуск {running.id} ещё идёт, после него будет один запуск"
        decision = MERGED
    else:
        text = f"отложен: запуск {running.id} по связи ещё идёт, начнётся после него"
        decision = DEFERRED
    for x in links:
        app.links.log(x.id, decision, text, source_run)


def start_pending(app: App, target: str) -> Run | None:
    """Отложенный повод получателя, если его запуск по связи закончился."""
    if _busy(app, target) is not None:
        return None
    db = app.db
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM link_pending WHERE target = ?", (target,)).fetchone()
        if row is not None:
            db.execute("DELETE FROM link_pending WHERE target = ?", (target,))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    if row is None:
        return None
    by_id = {x.id: x for x in app.links.incoming(target)}
    links = [by_id[i] for i in json.loads(row["links_json"]) if i in by_id]
    link = by_id.get(row["link_id"])
    if link is None or not links:
        return None  # связь удалили, пока повод ждал
    sources = {int(k): v for k, v in json.loads(row["sources_json"]).items() if int(k) in by_id}
    return launch_target(app, target, links, sources, link, row["source_run"], row["note"])


def launch_target(app: App, target: str, links: list[L.Link], sources: dict[int, str], link: L.Link,
                  source_run: str | None, note: str | None = None) -> Run | None:
    """Готовит запуск получателя и отдаёт его супервизору, как плановый. Не начался: запись
    `failed (not_started)` в истории получателя, failed в Telegram, решение в журнале связи."""
    from dawg_replay import launch

    try:
        prepared = launch.prepare(app, target, preset=link.preset, link_id=link.id, source_run=source_run,
                                  link_sources=sources)
    except ReplayError as e:
        if e.code == "parallel_limit":
            _spawn_waiting(app, target, links, sources, link, source_run, note)
            for x in links:
                app.links.log(x.id, QUEUED, "ждёт свободного места для запуска (лимит одновременных запусков)",
                              source_run)
            return None
        return _not_started(app, target, links, link, source_run, e)
    _after_prepared(app, prepared, links, source_run, note)
    _spawn(app, prepared)
    return prepared.run


def _after_prepared(app: App, prepared: Prepared, links: list[L.Link], source_run: str | None,
                    note: str | None) -> None:
    run = prepared.run
    src = _source(app, source_run)
    if src is not None:
        _run_log(app, run.id, f"Запущен после: {_title(app, src.script_name)}, запуск {src.id}")
    if note:
        _run_log(app, run.id, note, "warn")
    for x in links:
        app.links.log(x.id, STARTED, f"запущен: {run.id}", source_run, run.id)


def _not_started(app: App, target: str, links: list[L.Link], link: L.Link, source_run: str | None,
                 error: ReplayError) -> Run | None:
    from dawg_replay import launch

    if error.code in ("run_skipped", "run_cancelled"):
        # Запись уже есть: не дождался слота или отменён в очереди.
        rec_id = error.details.get("run") if isinstance(error.details, dict) else None
        for x in links:
            app.links.log(x.id, NOT_STARTED, f"не запущен: {error.message}", source_run, rec_id)
        return None
    rec = launch.record_not_started(app, None, target, link.preset, error, link_id=link.id, source_run=source_run)
    for x in links:
        app.links.log(x.id, NOT_STARTED, f"не запущен: {error.message}", source_run, rec.id if rec else None)
    return rec


def _spawn_supervisor(app: App, prepared: Prepared) -> None:
    from dawg_replay import launch

    launch.spawn_supervisor(app, prepared)


# Подменяется в тестах: запуск получателя идёт в этом же процессе.
_spawn: Callable[[App, Prepared], None] = _spawn_supervisor


def _detached_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def _spawn_detached(app: App, args: list[str], log_name: str) -> subprocess.Popen | None:
    argv = [sys.executable, "-m", "dawg_replay.chain", "--home", str(app.paths.home), *args]
    try:
        with open(app.paths.home / log_name, "ab") as log:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **_detached_kwargs())
    except OSError:
        return None
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _spawn_waiting_process(app: App, target: str, links: list[L.Link], sources: dict[int, str], link: L.Link,
                           source_run: str | None, note: str | None) -> None:
    """Лимит одновременных запусков занят: отдельный процесс ждёт слота (как плановый запуск) и ведёт запуск."""
    job = {"target": target, "links": [x.id for x in links], "sources": {str(k): v for k, v in sources.items()},
           "link": link.id, "source_run": source_run, "note": note}
    if _spawn_detached(app, ["launch", json.dumps(job, ensure_ascii=False)], "chain.log") is None:
        _not_started(app, target, links, link, source_run,
                     ReplayError("start_failed", "не удалось запустить процесс, который ждёт свободного места"))


_spawn_waiting: Callable[..., None] = _spawn_waiting_process


def run_waiting(app: App, job: dict[str, Any]) -> Run | None:
    """Процесс ожидания слота: prepare с ожиданием, потом супервизор в этом же процессе."""
    from dawg_replay import launch

    target = job["target"]
    by_id = {x.id: x for x in app.links.incoming(target)}
    links = [by_id[i] for i in job["links"] if i in by_id]
    link = by_id.get(job["link"])
    if link is None or not links:
        return None
    sources = {int(k): v for k, v in job["sources"].items()}
    try:
        prepared = launch.prepare(app, target, preset=link.preset, link_id=link.id, source_run=job["source_run"],
                                  link_sources=sources, wait=True)
    except ReplayError as e:
        return _not_started(app, target, links, link, job["source_run"], e)
    _after_prepared(app, prepared, links, job["source_run"], job.get("note"))
    return launch.supervise(app, prepared.run.id, prepared.payload())


# --- сроки кругов «после всех» ------------------------------------------------------------


def tick(app: App, nested: bool = False) -> list[str]:
    """Закрывает круги с вышедшим сроком и запускает отложенные поводы, чей получатель уже не идёт.
    Возвращает получателей, по которым принято решение."""
    if not nested:
        if getattr(_local, "busy", False):
            return []
        _local.busy = True
    try:
        done = []
        now = _iso(_now())
        for row in app.db.execute("SELECT target FROM link_rounds WHERE deadline <= ?", (now,)).fetchall():
            if _expire(app, row["target"], now):
                done.append(row["target"])
        for row in app.db.execute("SELECT target FROM link_pending").fetchall():
            if start_pending(app, row["target"]) is not None:
                done.append(row["target"])
        return done
    finally:
        if not nested:
            _local.busy = False


def _expire(app: App, target: str, now: str) -> bool:
    db = app.db
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM link_rounds WHERE target = ? AND deadline <= ?", (target, now)).fetchone()
        if row is not None:
            db.execute("DELETE FROM link_rounds WHERE target = ?", (target,))
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    if row is None:
        return False  # другой процесс успел
    starting = [x for x in app.links.incoming(target) if x.starts and x.enabled]
    if not starting:
        return True  # связи убрали, пока шёл круг
    arrived = {int(k): v for k, v in json.loads(row["arrived_json"] or "{}").items()}
    by_id = {x.id: x for x in starting}
    link = by_id.get(row["last_link"]) or starting[0]
    lacking = ", ".join(_title(app, x.source) for x in starting if x.id not in arrived)
    deadline = row["deadline"]
    late = (parse_ts(now) - parse_ts(deadline)).total_seconds() > LATE_SECONDS  # type: ignore[operator]
    mode = app.links.mode(target)
    when = moment(deadline)
    if mode.on_timeout == "run":
        note = None
        if late:
            note = f"Срок ожидания вышел в {when}, запущен позже: компьютер был выключен."
        for x in starting:
            if x.id not in arrived:
                app.links.log(x.id, SKIPPED, f"срок ожидания вышел в {when}: запуск с тем, что есть", row["last_run"])
        trigger(app, target, starting, {k: v for k, v in arrived.items() if k in by_id}, link, row["last_run"], note)
        return True
    text = f"не дождался источников ({lacking}): срок ожидания вышел в {when}"
    if late:
        text += ", обработан позже: компьютер был выключен"
    from dawg_replay import launch

    rec = launch.record_not_started(app, None, target, link.preset, ReplayError("link_timeout", text),
                                    link_id=link.id, source_run=row["last_run"], reason=REASON_SKIPPED)
    for x in starting:
        app.links.log(x.id, SKIPPED, f"пропущен: {text}", row["last_run"], rec.id if rec else None)
    return True


def next_deadline(app: App) -> datetime | None:
    row = app.db.execute("SELECT MIN(deadline) FROM link_rounds").fetchone()
    return parse_ts(row[0]) if row and row[0] else None


# --- процесс ожидания сроков ----------------------------------------------------------------


def _waiter_alive(path: Path) -> bool:
    try:
        info = json.loads(path.read_text("utf-8"))
        return procinfo.same_process(int(info["pid"]), info.get("started"))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def ensure_waiter(app: App) -> None:
    """Поднимает процесс, который обработает срок круга без открытого UI, если его ещё нет."""
    if os.environ.get(ENV_WAITER, "").lower() == "off":
        return
    if _waiter_alive(app.paths.home / WAITER_FILE):
        return
    _spawn_detached(app, ["wait"], "chain.log")


def wait_loop(home: Path, poll: float = WAITER_POLL_SECONDS, sleep: Callable[[float], None] = time.sleep) -> None:
    """Процесс ожидания: проверяет сроки, пока есть открытые круги. Время сверяет по часам, а не по
    таймеру: после сна компьютера срок срабатывает в пределах минуты."""
    from dawg_replay.app import App

    marker = home / WAITER_FILE
    pid = os.getpid()
    try:
        marker.write_text(json.dumps({"pid": pid, "started": procinfo.start_time(pid)}), "utf-8")
    except OSError:
        pass
    try:
        while True:
            with App(home) as app:
                tick(app)
                nxt = next_deadline(app)
            if nxt is None:
                return
            left = (nxt - _now()).total_seconds()
            sleep(max(1.0, min(poll, left + 1)))
    finally:
        try:
            info = json.loads(marker.read_text("utf-8"))
            if info.get("pid") == pid:
                marker.unlink()
        except (OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    from dawg_replay.app import App

    parser = argparse.ArgumentParser(prog="dawg_replay.chain")
    parser.add_argument("--home", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wait")
    p = sub.add_parser("launch")
    p.add_argument("job")
    args = parser.parse_args(argv)
    home = Path(args.home)
    if args.cmd == "wait":
        wait_loop(home)
        return 0
    with App(home) as app:
        run = run_waiting(app, json.loads(args.job))
    if run is not None:
        print(f"запуск {run.id}: {run.label}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
