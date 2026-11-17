"""Команды CLI запуска проекта: project run, runs, status, stop, retry, schedule."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from typing import Any

from dawg_replay import project_runs as P
from dawg_replay.app import App
from dawg_replay.cli_run import EXIT_RUN_FAILED, _handler_cmd
from dawg_replay.cli_schedule import STATES, spec_from_args
from dawg_replay.cli_store import Handler
from dawg_replay.errors import ReplayError
from dawg_replay.schedules import EVERY_HOURS

FOLLOW_POLL = 1.0


def _titles(app: App, pr: P.ProjectRun) -> dict[str, str]:
    try:
        project = app.projects.load(pr.project, pr.version).project
    except ReplayError:
        return {s.node: s.node for s in pr.steps}
    return {s.node: P.node_title(app, project, s.node) for s in pr.steps}


def _step_line(title: str, s: P.Step) -> str:
    if s.each and s.rows:
        extra = f", строк {s.summary()['text']}"
    else:
        extra = f": {s.reason}" if s.reason and s.status not in (P.OK, P.STEP_RUNNING) else ""
    run = f"  [{s.run_id}]" if s.run_id else ""
    return f"  {title}: {s.title}{extra}{run}"


def _row_lines(s: P.Step) -> list[str]:
    """Строки прохода, которые не дошли до «готово»: подпись, статус, причина, запуск."""
    out = []
    for r in s.rows:
        if r.status in (P.OK, P.PENDING, P.STARTING, P.STEP_RUNNING):
            continue
        why = f": {r.reason}" if r.reason else ""
        run = f"  [{r.run_id}]" if r.run_id else ""
        out.append(f"    строка {r.index + 1} «{r.label}»: {r.title}{why}{run}")
    return out


def _show(app: App, pr: P.ProjectRun) -> str:
    titles = _titles(app, pr)
    head = f"{pr.id}  {pr.project} v{pr.version}  {pr.label}  {pr.created_at}"
    if pr.trigger == "schedule":
        head += "  [по расписанию]"
    if pr.retry_of:
        head += f"  [перезапуск {pr.retry_of}]"
    lines = [head]
    if pr.error:
        lines.append(f"  {pr.error}")
    for s in pr.steps:
        lines.append(_step_line(titles[s.node], s))
        lines += _row_lines(s)
    return "\n".join(lines)


def _follow(app: App, pr: P.ProjectRun, args: argparse.Namespace) -> P.ProjectRun:
    """Ждёт конца запуска проекта и печатает смену статусов шагов. Ctrl+C останавливает проект."""
    interrupted = threading.Event()
    previous = {}

    def on_signal(signum: int, frame: Any) -> None:
        if not interrupted.is_set():
            print("\nостанавливаю запуск проекта...", file=sys.stderr, flush=True)
        interrupted.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            previous[sig] = signal.signal(sig, on_signal)
    seen: dict[str, Any] = {}
    titles = _titles(app, pr)
    stopped = False
    try:
        while True:
            for s in pr.steps:
                mark = (s.status, s.summary()["text"] if s.each else None)
                if seen.get(s.node) != mark:
                    seen[s.node] = mark
                    if not args.json and s.status != P.PENDING:
                        print(_step_line(titles.get(s.node, s.node), s), flush=True)
            if not pr.running:
                return pr
            if interrupted.is_set() and not stopped:
                stopped = True
                try:
                    P.stop(app, pr.id)
                except ReplayError:
                    pass
            time.sleep(FOLLOW_POLL)
            P.advance(app, pr.id)  # ведущий может быть ещё не поднят: решения не ждут его
            pr = P.get(app, pr.id)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _result(app: App, pr: P.ProjectRun, args: argparse.Namespace) -> tuple[dict, str]:
    if args.follow:
        pr = _follow(app, pr, args)
        args._exit_code = EXIT_RUN_FAILED if pr.status in (P.FAILED, P.CANCELLED) else 0
        return {"project_run": pr.to_dict()}, f"итог: {pr.label}\n" + _show(app, pr)
    text = (f"запуск проекта {pr.id} идёт в фоне ({pr.project} v{pr.version})\n"
            f"  ход: dawg-replay project status {pr.id}\n"
            f"  остановить: dawg-replay project stop {pr.id}")
    return {"project_run": pr.to_dict()}, text


def _run(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    notify = "always" if args.notify else ("never" if args.no_notify else None)
    try:
        pr = P.start(app, args.name, schedule_id=args.schedule, notify=notify)
    except ReplayError as e:
        if args.schedule and e.code in ("project_busy", "project_invalid") and (e.details or {}).get("run") is not None:
            args._exit_code = EXIT_RUN_FAILED
            skipped = P.get(app, e.details["run"])
            return {"project_run": skipped.to_dict(), "skipped": True}, f"запуск проекта {skipped.id}: {skipped.error}"
        raise
    return _result(app, pr, args)


def _retry(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    return _result(app, P.retry(app, args.id), args)


def _stop(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    pr = P.stop(app, args.id)
    text = f"запуск проекта {pr.id}: {pr.label}" if not pr.running else (
        f"запуск проекта {pr.id} останавливается: идущие шаги отменяются, остальные пропущены")
    return {"project_run": pr.to_dict()}, text


def _status(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    from dawg_replay import launch

    launch.recover_lost(app)
    pr = P.get(app, args.id)
    return {"project_run": pr.to_dict()}, _show(app, pr)


def _runs(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    from dawg_replay import launch

    app.projects.get(args.name)
    launch.recover_lost(app)
    items = P.history(app, args.name, args.limit)
    lines = []
    for pr in items:
        c = pr.counts()
        counted = sum(v for k, v in c.items() if k != P.OFF_BRANCH)
        good = c.get(P.OK, 0) + c.get(P.PARTIAL, 0)
        lines.append(f"{pr.id}  {pr.label:<16} v{pr.version}  {pr.created_at}  шагов {good} из {counted}"
                     + ("  [по расписанию]" if pr.trigger == "schedule" else ""))
    return {"runs": [pr.to_dict() for pr in items]}, "\n".join(lines) or "запусков проекта нет"


def _schedule_line(d: dict[str, Any]) -> str:
    nxt = d["next_run"].replace("T", " ") if d.get("next_run") else "нет"
    return f"{d['id']}  {d['project']}  {d['when']}, следующий {nxt}  [{STATES.get(d['state'], d['state'])}]"


def _schedule_add(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    s = app.schedules.add_project(args.name, spec_from_args(args))
    d = s.to_dict()
    log = app.schedules.log_path(s.id)
    text = (f"расписание {s.id}: проект {args.name}, {d['when']}\n"
            f"  следующий запуск: {(d['next_run'] or 'нет').replace('T', ' ')}\n"
            f"  вывод: {log}\n"
            f"  снять: dawg-replay project schedule remove {s.id}")
    return {"schedule": {**d, "state": "ok", "log": str(log)}, "scheduler": app.schedules.backend.kind}, text


def _schedule_list(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    data = app.schedules.project_list(args.name)
    lines = [_schedule_line(d) for d in data["schedules"]]
    if data["error"]:
        lines.append(f"планировщик недоступен: {data['error']}")
    return data, "\n".join(lines) or "регулярных запусков проектов нет"


def _schedule_remove(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    app.schedules.project_get(args.id)
    r = app.schedules.remove(args.id)
    return r, f"расписание {args.id} снято"


def register(g: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    """Добавляет команды запуска к `dawg-replay project`."""

    def add(parent: Any, name: str, handler: Handler, help: str, description: str | None = None) -> argparse.ArgumentParser:
        p = parent.add_parser(name, parents=[common], help=help, description=description or help)
        p.set_defaults(func=_handler_cmd(handler))  # код выхода 3, если запуск проекта упал
        return p

    p = add(g, "run", _run, "запустить проект",
            "Запускает текущую версию проекта: узлы без входящих стрелок сразу, остальные по стрелкам. "
            "Запуск идёт в фоне и без открытого интерфейса; с --follow команда ждёт конца и показывает шаги, "
            "Ctrl+C останавливает проект. Пока идёт запуск проекта, второй не начинается.")
    p.add_argument("name", help="имя проекта")
    p.add_argument("--follow", action="store_true", help="ждать конца и показывать шаги")
    p.add_argument("--schedule", metavar="ID", help=argparse.SUPPRESS)
    n = p.add_mutually_exclusive_group()
    n.add_argument("--notify", action="store_true", help="сообщить в Telegram об итоге в любом случае")
    n.add_argument("--no-notify", action="store_true", help="не сообщать об успешном итоге")

    p = add(g, "runs", _runs, "история запусков проекта")
    p.add_argument("name", help="имя проекта")
    p.add_argument("--limit", type=int, default=20)

    p = add(g, "status", _status, "запуск проекта: статус каждого шага и причины пропуска")
    p.add_argument("id", help="id запуска проекта")

    p = add(g, "stop", _stop, "остановить запуск проекта: идущие шаги отменяются, остальные пропускаются")
    p.add_argument("id", help="id запуска проекта")

    p = add(g, "retry", _retry, "перезапустить с упавшего",
            "Новый запуск по той же версии проекта: шаги «готово» не повторяются, их файлы берутся из прошлого "
            "запуска, остальные идут заново. Запустить по текущей версии: dawg-replay project run.")
    p.add_argument("id", help="id прошлого запуска проекта")
    p.add_argument("--follow", action="store_true", help="ждать конца и показывать шаги")

    s = g.add_parser("schedule", parents=[common], help="регулярные запуски проекта",
                     description="Регулярный запуск проекта через системный планировщик, как у скриптов. "
                                 "Правила Telegram расписания: dawg-replay telegram set --schedule ID.",
                     ).add_subparsers(dest="project_schedule_command", metavar="<действие>", required=True)
    p = add(s, "add", _schedule_add, "поставить регулярный запуск проекта",
            "Время местное. Компьютер должен быть включён, а пользователь в системе.")
    p.add_argument("name", help="имя проекта")
    p.add_argument("--daily", metavar="ЧЧ:ММ", help="каждый день в это время")
    p.add_argument("--weekly", nargs=2, metavar=("ДЕНЬ", "ЧЧ:ММ"), help="раз в неделю: mon..sun или пн..вс")
    p.add_argument("--every-hours", type=int, metavar="N",
                   help=f"каждые N часов с полуночи, N из {', '.join(map(str, EVERY_HOURS))}")
    p.add_argument("--cron", metavar="ВЫРАЖЕНИЕ", help="выражение cron из пяти полей (только macOS и Linux)")
    p = add(s, "list", _schedule_list, "регулярные запуски проектов и сверка с планировщиком")
    p.add_argument("name", nargs="?", help="только этого проекта")
    p = add(s, "remove", _schedule_remove, "снять регулярный запуск проекта")
    p.add_argument("id")
