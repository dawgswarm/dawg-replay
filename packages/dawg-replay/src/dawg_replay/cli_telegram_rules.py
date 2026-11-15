"""`dawg-replay telegram set`: о чём писать в Telegram, в общих настройках, у скрипта или у расписания."""

from __future__ import annotations

import argparse
from typing import Any

from dawg_replay import tg_rules
from dawg_replay.app import App
from dawg_replay.cli_store import run_handler
from dawg_replay.errors import ReplayError


def _value_text(key: str, value: Any) -> str:
    if key == "done_manual":
        return {"always": "всегда", "long": "если дольше порога", "never": "никогда"}[value]
    if key == "long_minutes":
        return f"{value} мин"
    return "вкл" if value else "выкл"


def _owner(app: App, args: argparse.Namespace) -> tuple[str, str] | None:
    if args.script:
        app.registry.get(args.script)
        return "script", args.script
    if args.schedule:
        app.schedules.get(args.schedule)
        return "schedule", args.schedule
    return None


def _rows(app: App, owner: tuple[str, str] | None) -> list[dict[str, Any]]:
    if owner is None:
        rules = tg_rules.general(app.config)
        return [{"key": k, "value": rules[k], "own": None, "title": tg_rules.EVENTS[k][0]} for k in tg_rules.KEYS]
    kind, name = owner
    script = app.schedules.get(name).script if kind == "schedule" else None
    return tg_rules.layers(app, kind, name, script)


def _set(app: App, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    owner = _owner(app, args)
    if args.event is not None:
        if args.value is None:
            raise ReplayError("bad_args", f"нужно значение: dawg-replay telegram set {args.event} on|off")
        value = args.value
        if owner is None:
            if value == tg_rules.INHERIT:
                raise ReplayError("bad_args", "inherit бывает только у скрипта (--script) или расписания (--schedule)")
            tg_rules.set_general(app, args.event, value)
        else:
            tg_rules.set_overrides(app, owner[0], owner[1], {args.event: value})
    rows = _rows(app, owner)
    where = "общие настройки" if owner is None else (
        f"скрипт {owner[1]}" if owner[0] == "script" else f"расписание {owner[1]}")
    lines = [f"Telegram, {where}:"]
    for r in rows:
        mark = "" if owner is None else (" (своё)" if r["own"] is not None else " (наследуется)")
        lines.append(f"  {r['key']:<15} {_value_text(r['key'], r['value'])}{mark}    {r['title']}")
    lines.append("Значения: on|off; done_manual: always|long|never; long_minutes: число минут; "
                 "у скрипта и расписания inherit снимает своё значение.")
    data = {"scope": owner[0] if owner else "general", "name": owner[1] if owner else None,
            "rules": {r["key"]: r["value"] for r in rows},
            "own": {r["key"]: r["own"] for r in rows if r["own"] is not None}}
    return data, "\n".join(lines)


def add_set(tg_sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = tg_sub.add_parser(
        "set",
        parents=[common],
        help="о чём писать в Telegram",
        description=(
            "Без аргументов показывает правила. События: failed (запуск упал), ask (скрипт задал вопрос), "
            "done_scheduled (запуск по расписанию закончился), done_manual (ручной запуск закончился: "
            "always, long или never), long_minutes (порог долгого ручного запуска), suspicious (похоже, сайт "
            "поменялся), attach_main (приложить главный файл итога). С --script или --schedule правило "
            "задаётся только для них, inherit возвращает общее."
        ),
    )
    p.add_argument("event", nargs="?", choices=tg_rules.KEYS, metavar="событие")
    p.add_argument("value", nargs="?", metavar="значение")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--script", metavar="NAME", help="правило только для этого скрипта")
    g.add_argument("--schedule", metavar="ID", help="правило только для этого расписания")
    p.set_defaults(func=lambda args: run_handler(_set, args))
