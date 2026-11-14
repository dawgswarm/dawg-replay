"""Команды CLI регулярных запусков: schedule add, list, remove, sync."""

from __future__ import annotations

import argparse
from typing import Any

from dawg_replay.app import App
from dawg_replay.cli_store import Handler, run_handler
from dawg_replay.errors import ReplayError
from dawg_replay.schedules import EVERY_HOURS, Spec

STATES = {"ok": "в планировщике", "missing": "задача пропала из планировщика (dawg-replay schedule sync)", "unknown": "планировщик недоступен"}


def spec_from_args(args: argparse.Namespace) -> Spec:
    given = [x for x in (args.daily, args.weekly, args.every_hours, args.cron) if x is not None]
    if len(given) != 1:
        raise ReplayError("bad_schedule", "укажите одно из: --daily ЧЧ:ММ, --weekly ДЕНЬ ЧЧ:ММ, --every-hours N, --cron ВЫРАЖЕНИЕ")
    if args.daily is not None:
        return Spec.daily(args.daily)
    if args.weekly is not None:
        return Spec.weekly(args.weekly[0], args.weekly[1])
    if args.every_hours is not None:
        return Spec.every_hours(args.every_hours)
    return Spec.from_cron(args.cron)


def _line(d: dict[str, Any]) -> str:
    nxt = d["next_run"].replace("T", " ") if d.get("next_run") else "нет"
    return f"{d['id']}  {d['script']} / {d['preset']}  {d['when']}, следующий {nxt}  [{STATES.get(d['state'], d['state'])}]"


def _add(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    s = app.schedules.add(args.script, args.preset, spec_from_args(args))
    d = s.to_dict()
    log = app.schedules.log_path(s.id)
    text = (
        f"расписание {s.id}: {args.script} с пресетом {args.preset}, {d['when']}\n"
        f"  следующий запуск: {(d['next_run'] or 'нет').replace('T', ' ')}\n"
        f"  вывод: {log}\n"
        f"  снять: dawg-replay schedule remove {s.id}"
    )
    return {"schedule": {**d, "state": "ok", "log": str(log)}, "scheduler": app.schedules.backend.kind}, text


def _list(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    data = app.schedules.list(args.script)
    lines = [_line(d) for d in data["schedules"]]
    if data["error"]:
        lines.append(f"планировщик недоступен: {data['error']}")
    for o in data["orphans"]:
        lines.append(f"{o['id']}  задача осталась в планировщике без расписания в приложении: dawg-replay schedule remove {o['id']}")
    return data, "\n".join(lines) or "регулярных запусков нет"


def _remove(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    r = app.schedules.remove(args.id)
    return r, f"расписание {args.id} снято" + (" (задача без записи в приложении)" if r["was_orphan"] else "")


def _sync(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    r = app.schedules.sync()
    lines = [f"поставлено заново: {', '.join(r['reinstalled'])}"] if r["reinstalled"] else []
    if r["orphans_removed"]:
        lines.append(f"сняты задачи без расписания: {', '.join(r['orphans_removed'])}")
    return r, "\n".join(lines) or "планировщик и приложение совпадают"


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    def add(parent: Any, name: str, handler: Handler, help: str, description: str | None = None) -> argparse.ArgumentParser:
        p = parent.add_parser(name, parents=[common], help=help, description=description or help)
        p.set_defaults(func=lambda args, h=handler: run_handler(h, args))
        return p

    g = sub.add_parser(
        "schedule",
        parents=[common],
        help="регулярные запуски через системный планировщик",
        description="Регулярные запуски по пресету: cron на macOS и Linux, Планировщик задач на Windows. "
        "Задача вызывает dawg-replay run <скрипт> --preset <пресет> --follow, вывод пишется в лог в каталоге данных.",
    ).add_subparsers(dest="schedule_command", metavar="<действие>", required=True)

    p = add(g, "add", _add, "поставить регулярный запуск скрипта по пресету",
            "Время местное. Компьютер должен быть включён, а пользователь в системе: так задаче доступен ключ DAWG.")
    p.add_argument("script")
    p.add_argument("--preset", required=True, help="пресет скрипта")
    p.add_argument("--daily", metavar="ЧЧ:ММ", help="каждый день в это время")
    p.add_argument("--weekly", nargs=2, metavar=("ДЕНЬ", "ЧЧ:ММ"), help="раз в неделю: mon..sun или пн..вс")
    p.add_argument("--every-hours", type=int, metavar="N",
                   help=f"каждые N часов с полуночи, N из {', '.join(map(str, EVERY_HOURS))}")
    p.add_argument("--cron", metavar="ВЫРАЖЕНИЕ", help="выражение cron из пяти полей (только macOS и Linux)")

    p = add(g, "list", _list, "регулярные запуски и сверка с планировщиком")
    p.add_argument("script", nargs="?", help="только этого скрипта")

    p = add(g, "remove", _remove, "снять регулярный запуск")
    p.add_argument("id")

    add(g, "sync", _sync, "поставить пропавшие задачи заново и снять задачи без расписания")
