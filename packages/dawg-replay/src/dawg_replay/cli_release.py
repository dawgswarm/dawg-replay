"""Остаточное окно (ТЗ 5.6) в CLI: команда `release` и предупреждение для runs и logs.

Процесс скрипта мог умереть между отправкой запроса на браузер и ответом. Тогда
id браузера неизвестен, и страховка только перечисляет сессии ключа, созданные
в это время (`suspects` в insurance.json). Освобождает их пользователь явно:
на том же ключе живут dawg-mcp и другие запуски.
"""

from __future__ import annotations

import argparse
from typing import Any

from dawg_replay import insurance
from dawg_replay.app import App
from dawg_replay.cli_run import _handler_cmd
from dawg_replay.errors import ReplayError

EXIT_RELEASE_FAILED = 1


def suspects_for(app: App, run_id: str) -> list[dict[str, Any]]:
    return insurance.pending_suspects(app.paths.run_dir(run_id))


def warning_lines(app: App, run_id: str) -> list[str]:
    suspects = suspects_for(app, run_id)
    if not suspects:
        return []
    return [
        insurance.warning_line(suspects),
        f"освободить, если эти браузеры не нужны: dawg-replay release {run_id} --all-suspects "
        "(или по одной: dawg-replay release <run_id> <session_id>)",
    ]


def _release(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    if bool(args.session_id) == bool(args.all_suspects):
        raise ReplayError("bad_arguments", "укажите session_id или --all-suspects")
    rec = app.runs.get(args.run_id)
    if rec.running:
        raise ReplayError("run_running", f"запуск {rec.id} ещё идёт, браузеры после него проверит страховка")
    run_dir = app.paths.run_dir(rec.id)
    if not suspects_for(app, rec.id) and args.all_suspects:
        raise ReplayError("no_suspects", f"у запуска {rec.id} нет неосвобождённых подозрительных сессий")
    key = app.secrets.get_api_key()
    if not key:
        raise ReplayError("no_api_key", "ключ DAWG не задан: dawg-replay config set-key")
    base_url = app.config.base_url or None
    try:
        results = insurance.release_suspects(
            run_dir,
            None if args.all_suspects else [args.session_id],
            lambda sid: insurance.release_session(key, sid, base_url),
        )
    except insurance.NotASuspect as e:
        raise ReplayError("not_a_suspect", str(e)) from None
    if any(not r.released for r in results):
        args._exit_code = EXIT_RELEASE_FAILED
    lines = [
        f"{r.session_id}: освобождена" if r.released else f"{r.session_id}: не освобождена: {r.error}"
        for r in results
    ] or ["эта сессия уже освобождена"]
    left = suspects_for(app, rec.id)
    data = {"run_id": rec.id, "results": [r.to_dict() for r in results], "suspects_left": left}
    return data, "\n".join(lines)


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    help_text = "освободить браузер из остаточного окна запуска"
    p = sub.add_parser(
        "release",
        parents=[common],
        help=help_text,
        description=(
            "Освобождает сессии DAWG, о которых запуск предупредил «возможно, остался браузер». "
            "Можно освободить только сессии из списка этого запуска."
        ),
    )
    p.add_argument("run_id")
    p.add_argument("session_id", nargs="?", help="одна сессия из списка запуска")
    p.add_argument("--all-suspects", action="store_true", help="все неосвобождённые сессии из списка запуска")
    p.set_defaults(func=_handler_cmd(_release))
