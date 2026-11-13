"""Команды для разработки: `dawg-replay dev kit-env`."""

from __future__ import annotations

import argparse
import shlex
import sys
from typing import Any

from dawg_replay import devkit
from dawg_replay.app import App
from dawg_replay.cli_store import run_handler
from dawg_replay.errors import ReplayError


def _kit_env(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    source = args.source or app.config.dev_kit_source
    if not source:
        raise ReplayError(
            "dev_kit_source_not_set",
            "не задан путь к kit: --source packages/dawg-replay-kit или dawg-replay config set dev_kit_source <путь>",
        )
    kit = devkit.ensure(source, app.paths.dev_kit)
    command = ["uv", "run", *kit.uv_args]
    data: dict[str, Any] = {"env": kit.env, "command": command, "find_links": str(kit.find_links)}
    if sys.platform == "win32":
        export = f"$env:{devkit.ENV_FIND_LINKS} = '{kit.find_links}'"
    else:
        export = f"export {devkit.ENV_FIND_LINKS}={shlex.quote(str(kit.find_links))}"
    text = (
        f"{export}\n"
        f"# затем автономный запуск со свежим kit:\n"
        f"# {' '.join(command)} script.py --help"
    )
    return data, text


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser("dev", parents=[common], help="инструменты разработки", description="инструменты разработки")
    g = p.add_subparsers(dest="dev_command", metavar="<действие>", required=True)
    help = "собрать локальный kit и показать окружение для автономного uv run script.py"
    k = g.add_parser("kit-env", parents=[common], help=help, description=help)
    k.add_argument("--source", help="каталог packages/dawg-replay-kit (по умолчанию из dev_kit_source)")
    k.set_defaults(func=lambda args: run_handler(_kit_env, args))
