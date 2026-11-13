"""Командная строка dawg-replay. Логики здесь нет, только разбор аргументов и вывод."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from dawg_replay_kit.manifest import SUPPORTED_CONTRACTS, ManifestError, load_manifest

from dawg_replay import __version__, cli_run, cli_store

EXIT_OK = 0
EXIT_INVALID = 1


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _cmd_version(args: argparse.Namespace) -> int:
    if args.json:
        _print_json({"version": __version__, "contracts": list(SUPPORTED_CONTRACTS)})
    else:
        print(f"dawg-replay {__version__}")
        print(f"контракт скриптов: {', '.join(str(v) for v in SUPPORTED_CONTRACTS)}")
    return EXIT_OK


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.file)
    except ManifestError as e:
        if args.json:
            _print_json({"ok": False, "file": args.file, "errors": [i.to_dict() for i in e.issues]})
        else:
            print(f"{args.file}: манифест не прошёл проверку, ошибок: {len(e.issues)}", file=sys.stderr)
            for issue in e.issues:
                print(f"  {issue}", file=sys.stderr)
        return EXIT_INVALID

    if args.json:
        _print_json({"ok": True, "file": args.file, "manifest": manifest.to_dict(), "errors": []})
    else:
        print(f"{args.file}: ок")
        print(f"  {manifest.name} v{manifest.version}, контракт {manifest.contract}.{manifest.contract_minor}")
        print(f"  {manifest.title}")
        if manifest.params:
            print(f"  параметры: {', '.join(f'{p.name} ({p.type})' for p in manifest.params)}")
        else:
            print("  параметров нет")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    # --json работает и до, и после имени команды.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="машиночитаемый вывод")

    parser = argparse.ArgumentParser(
        prog="dawg-replay",
        description="Запуск скриптов сбора данных через браузеры DAWG.",
        parents=[common],
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="версия приложения и поддерживаемые версии контракта",
    )
    sub = parser.add_subparsers(dest="command", metavar="<команда>")

    p = sub.add_parser(
        "validate",
        parents=[common],
        help="проверить манифест скрипта без регистрации",
        description="Проверяет манифест и контракт скрипта. Скрипт не запускается.",
    )
    p.add_argument("file", help="путь к script.py")
    p.set_defaults(func=_cmd_validate)

    cli_store.register(sub, common)
    cli_run.register(sub, common)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    if args.version:
        return _cmd_version(args)
    if not args.command:
        parser.print_help()
        return EXIT_OK
    return args.func(args)
