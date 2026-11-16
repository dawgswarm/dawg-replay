"""Команды `dawg-replay link`: связи между скриптами, режим получателя, проверка схемы."""

from __future__ import annotations

import argparse
import re
from typing import Any

from dawg_replay import links as L
from dawg_replay.app import App
from dawg_replay.cli_store import Handler, run_handler
from dawg_replay.errors import ReplayError

AFTER_WORDS = {
    "ok": "после успешного",
    "partial": "после успешного или частичного",
    "any": "после любого исхода",
    "never": "не запускает, только файл",
}
MODE_WORDS = {"all": "после всех", "any": "после любого"}
TIMEOUT_WORDS = {"skip": "пропустить", "run": "запустить с тем, что есть"}


def parse_transfer(raw: str) -> L.Transfer:
    """`<файл>=<параметр>`: имя файла результата, шаблон или @main, справа параметр получателя."""
    file, sep, param = raw.rpartition("=")
    if not sep or not file.strip() or not param.strip():
        raise ReplayError("bad_link", f"`{raw}`: передача задаётся так: --file prices.csv=товары (или @main=товары)")
    return L.Transfer(file.strip(), param.strip().replace("-", "_"))


def parse_wait(raw: str) -> int:
    """Срок ожидания: `6h`, `90m`, `1d`; число без единицы это часы."""
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(d|h|m|min|ч|м|д)?\s*", raw or "", re.IGNORECASE)
    if not m:
        raise ReplayError("bad_link", f"срок ожидания вида 6h, 90m или 1d, получено {raw!r}")
    num = float(m.group(1).replace(",", "."))
    unit = (m.group(2) or "h").lower()
    mult = {"d": 86400, "д": 86400, "h": 3600, "ч": 3600, "m": 60, "min": 60, "м": 60}[unit]
    return int(num * mult)


def wait_text(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} сут."
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ч"
    return f"{seconds // 60} мин"


def link_line(link: L.Link) -> str:
    after = AFTER_WORDS[link.after]
    if link.after == "never" and link.cond == "partial":
        after += " (берёт и частичный)"
    files = ", ".join(t.label for t in link.files) or "без файлов"
    extra = []
    if link.preset:
        extra.append(f"пресет «{link.preset}»")
    if link.limit_per_day:
        extra.append(f"не больше {link.limit_per_day} в сутки")
    if not link.enabled:
        extra.append("выключена")
    tail = f"  [{', '.join(extra)}]" if extra else ""
    return f"{link.id}  {link.source} → {link.target}  {after}; {files}{tail}"


def _problems_text(problems: list[L.Problem]) -> list[str]:
    return [("ошибка: " if p.level == "error" else "предупреждение: ") + p.text for p in problems]


def _cond_starts(after: str, partial: bool) -> tuple[str, bool]:
    if after == "never":
        return ("partial" if partial else "ok"), False
    if partial:
        raise ReplayError("bad_link", "--partial только с --after never; для запуска после частичного: --after partial")
    return after, True


def _pick(app: App, args: argparse.Namespace) -> L.Link:
    ref = args.link
    if len(ref) > 2:
        raise ReplayError("bad_link", "связь задаётся id или парой «источник получатель»")
    return app.links.resolve(ref[0], ref[1] if len(ref) == 2 else None)


def _add(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    cond, starts = _cond_starts(args.after, args.partial)
    link, warnings = app.links.add(
        args.source, args.target, cond=cond, starts=starts,
        files=[parse_transfer(x) for x in args.file or ()], preset=args.preset,
        limit_per_day=args.limit_per_day, enabled=not args.disabled,
    )
    lines = [f"связь добавлена: {link_line(link)}", *_problems_text(warnings),
             "проверить всю схему: dawg-replay link check"]
    return {"link": link.to_dict(), "warnings": [w.to_dict() for w in warnings]}, "\n".join(lines)


def _list(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    if args.script:
        app.registry.get(args.script)
    items = app.links.list(args.script)
    return {"links": [x.to_dict() for x in items]}, "\n".join(link_line(x) for x in items) or "связей нет"


def _show(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    link = _pick(app, args)
    mode = app.links.mode(link.target)
    journal = app.links.journal(link.id, args.limit)
    problems = [p for p in app.links.check(link.target) if p.link_id in (link.id, None)]
    lines = [link_line(link),
             f"получатель {link.target}: {MODE_WORDS[mode.mode]}, ждать отстающего {wait_text(mode.wait_seconds)}, "
             f"по сроку {TIMEOUT_WORDS[mode.on_timeout]}"]
    lines += _problems_text(problems)
    if journal:
        lines.append("последние решения:")
        lines += [f"  {j['at'].replace('T', ' ')}  {j['text']}" for j in journal]
    data = {"link": link.to_dict(), "target_mode": mode.to_dict(), "journal": journal,
            "problems": [p.to_dict() for p in problems]}
    return data, "\n".join(lines)


def _set(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    link = _pick(app, args)
    kw: dict[str, Any] = {}
    if args.after is not None or args.partial:
        after = args.after or link.after
        kw["cond"], kw["starts"] = _cond_starts(after, args.partial)
    if args.no_files:
        kw["files"] = []
    elif args.file:
        kw["files"] = [parse_transfer(x) for x in args.file]
    if args.no_preset:
        kw["preset"] = None
    elif args.preset is not None:
        kw["preset"] = args.preset
    if args.no_limit:
        kw["limit_per_day"] = None
    elif args.limit_per_day is not None:
        kw["limit_per_day"] = args.limit_per_day
    if not kw:
        raise ReplayError("bad_link", "нечего менять: укажите --after, --file, --preset или --limit-per-day")
    link, warnings = app.links.update(link.id, **kw)
    lines = [f"связь изменена: {link_line(link)}", *_problems_text(warnings)]
    return {"link": link.to_dict(), "warnings": [w.to_dict() for w in warnings]}, "\n".join(lines)


def _remove(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    link = app.links.remove(_pick(app, args).id)
    return {"removed": link.to_dict()}, f"связь {link.source} → {link.target} удалена"


def _enable(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    link = app.links.set_enabled(_pick(app, args).id, True)
    return {"link": link.to_dict()}, f"связь {link.source} → {link.target} включена"


def _disable(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    link = app.links.set_enabled(_pick(app, args).id, False)
    return {"link": link.to_dict()}, f"связь {link.source} → {link.target} выключена"


def _mode(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    if args.mode is None and args.wait is None and args.on_timeout is None:
        app.registry.get(args.target)
        m = app.links.mode(args.target)
    else:
        m = app.links.set_mode(args.target, args.mode, parse_wait(args.wait) if args.wait is not None else None,
                               args.on_timeout)
    starting = [x for x in app.links.incoming(args.target) if x.starts]
    text = (f"{args.target}: {MODE_WORDS[m.mode]}, ждать отстающего {wait_text(m.wait_seconds)}, "
            f"по сроку {TIMEOUT_WORDS[m.on_timeout]}")
    if len(starting) < 2:
        text += "\n  режим важен, когда у скрипта две связи с запуском и больше"
    return {"target_mode": m.to_dict()}, text


def _check(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    problems = app.links.check(args.script)
    errors = [p for p in problems if p.level == "error"]
    if errors:
        raise ReplayError("link_check_failed", "схема связей с ошибками:\n" + "\n".join(f"  {x}" for x in _problems_text(problems)),
                          [p.to_dict() for p in problems])
    count = len(app.links.list(args.script))
    text = "\n".join(_problems_text(problems)) or f"связей: {count}, ошибок нет"
    return {"problems": [p.to_dict() for p in problems], "links": count}, text


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    def add(parent: Any, name: str, handler: Handler, help: str, description: str | None = None) -> argparse.ArgumentParser:
        p = parent.add_parser(name, parents=[common], help=help, description=description or help)
        p.set_defaults(func=lambda args, h=handler: run_handler(h, args))
        return p

    g = sub.add_parser(
        "link",
        parents=[common],
        help="связи между скриптами: запускать после, передать файл результата",
        description="Связь соединяет скрипт-источник и скрипт-получатель: получатель запускается после источника "
        "и/или получает файл его результата в параметр table или file.",
    ).add_subparsers(dest="link_command", metavar="<действие>", required=True)

    def link_ref(p: argparse.ArgumentParser) -> None:
        p.add_argument("link", nargs="+", metavar="ID|ИСТОЧНИК ПОЛУЧАТЕЛЬ", help="id связи или пара скриптов")

    def options(p: argparse.ArgumentParser, adding: bool) -> None:
        p.add_argument("--after", choices=L.AFTER_CHOICES, default="ok" if adding else None,
                       help="когда запускать получателя: ok после успешного" + (" (по умолчанию)" if adding else "") +
                       ", partial после успешного или частичного, any после любого исхода, never не запускать, только файл")
        p.add_argument("--partial", action="store_true", help="с --after never: брать и частичный результат")
        p.add_argument("--file", action="append", metavar="ФАЙЛ=ПАРАМЕТР",
                       help="передать файл результата (имя, шаблон prices-*.csv или @main) в параметр получателя; "
                       "можно несколько" + ("" if adding else ", заменяют прежние передачи"))
        p.add_argument("--preset", help="пресет получателя для запуска по связи")
        p.add_argument("--limit-per-day", type=int, metavar="N", help="не больше N запусков по связи в сутки")

    p = add(g, "add", _add, "добавить связь")
    p.add_argument("source", help="скрипт-источник")
    p.add_argument("target", help="скрипт-получатель")
    options(p, adding=True)
    p.add_argument("--disabled", action="store_true", help="добавить выключенной")

    p = add(g, "list", _list, "связи (все или скрипта)")
    p.add_argument("script", nargs="?", help="только связи этого скрипта")

    p = add(g, "show", _show, "связь, режим получателя и последние решения")
    link_ref(p)
    p.add_argument("--limit", type=int, default=20, help="сколько последних решений показать (20)")

    p = add(g, "set", _set, "изменить связь")
    link_ref(p)
    options(p, adding=False)
    p.add_argument("--no-files", action="store_true", help="убрать все передачи файлов")
    p.add_argument("--no-preset", action="store_true", help="запускать без пресета")
    p.add_argument("--no-limit", action="store_true", help="снять предел запусков в сутки")

    p = add(g, "remove", _remove, "удалить связь")
    link_ref(p)
    p = add(g, "enable", _enable, "включить связь")
    link_ref(p)
    p = add(g, "disable", _disable, "выключить связь: не запускает и не передаёт файлы")
    link_ref(p)

    p = add(g, "mode", _mode, "режим получателя с несколькими источниками",
            "all: запустить один раз, когда закончились все источники (по умолчанию); any: после каждого источника. "
            "Без аргументов показывает текущий режим.")
    p.add_argument("target", help="скрипт-получатель")
    p.add_argument("mode", nargs="?", choices=L.MODES, help="all или any")
    p.add_argument("--wait", metavar="СРОК", help="сколько ждать отстающего источника: 6h, 90m, 1d (6h)")
    p.add_argument("--on-timeout", choices=L.ON_TIMEOUT, help="по сроку: skip пропустить (по умолчанию) или run запустить с тем, что есть")

    p = add(g, "check", _check, "проверить схему без запуска: параметры, колонки, круги")
    p.add_argument("script", nargs="?", help="только связи этого скрипта")
