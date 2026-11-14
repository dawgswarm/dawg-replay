"""Команда `dawg-replay usage`: примерный расход минут браузеров DAWG по запускам (dawg_replay.minutes)."""

from __future__ import annotations

import argparse
from typing import Any

from dawg_replay import minutes
from dawg_replay.app import App
from dawg_replay.cli_store import run_handler
from dawg_replay.errors import ReplayError

MAX_DAYS = 366
NOTE = "Оценка по событиям запусков, точный расход и остаток в личном кабинете DAWG и на главной в интерфейсе."


def _usage(app: App, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if not 1 <= args.days <= MAX_DAYS:
        raise ReplayError("bad_value", f"--days: от 1 до {MAX_DAYS}")
    if args.script is not None:
        app.registry.get(args.script)  # понятная ошибка для неизвестного имени
    p = minutes.period(app, days=args.days, script=args.script)
    head = f"Минуты браузеров DAWG за {args.days} дн.: {minutes.fmt(p.minutes)}, запусков {p.runs}"
    lines = [head]
    if args.script is not None:
        for r in p.by_run:
            if r["sessions"]:
                tail = f", без закрытия {r['unclosed']}" if r["unclosed"] else ""
                lines.append(f"  {r['run']}  {r['label']:<18} {minutes.fmt(r['minutes'])}, браузеров {r['sessions']}{tail}")
    else:
        width = max((len(s.script) for s in p.scripts), default=0)
        for s in p.scripts:
            lines.append(f"  {s.script:<{width}}  {minutes.fmt(s.minutes)}, запусков {s.runs}, с браузером {s.with_browsers}")
    lines.append(NOTE)
    return p.to_dict(), "\n".join(lines)


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    help_text = "примерный расход минут браузеров DAWG по запускам и скриптам"
    p = sub.add_parser(
        "usage",
        parents=[common],
        help=help_text,
        description=(
            "Считает минуты браузеров DAWG по событиям session open и close в запусках за последние дни. "
            "Сессия без закрытия считается до конца запуска. Это оценка: биллинг DAWG считает по своим правилам."
        ),
    )
    p.add_argument("--script", metavar="NAME", help="только этот скрипт, с разбивкой по запускам")
    p.add_argument("--days", type=int, default=7, help="за сколько последних дней (7)")
    p.set_defaults(func=lambda args: run_handler(_usage, args))
