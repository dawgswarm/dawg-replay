"""Команда `dawg-replay ui`: локальный веб-интерфейс (ТЗ 6.4)."""

from __future__ import annotations

import argparse
import json
import sys

from dawg_replay.paths import default_home


def _ui(args: argparse.Namespace) -> int:
    from dawg_replay.web.server import serve
    from dawg_replay.web.security import Secrets

    def ready(s: Secrets) -> None:
        url = s.login_url()
        if args.json:
            print(json.dumps({"url": url, "port": s.port}, ensure_ascii=False), flush=True)
        else:
            print(f"dawg-replay ui: {url}", flush=True)
            print("Ссылка действует, пока работает этот сервер. Остановить: Ctrl+C, начатые здесь запуски будут отменены.", flush=True)

    if args.port is not None and not (1 <= args.port <= 65535):
        print("ошибка: порт должен быть от 1 до 65535", file=sys.stderr)
        return 2
    try:
        serve(default_home(), port=args.port, open_browser=not args.no_open, on_ready=ready)
    except KeyboardInterrupt:
        # uvicorn после штатной остановки по Ctrl+C заново поднимает SIGINT.
        print("dawg-replay ui остановлен", file=sys.stderr)
        return 130
    except OSError as e:
        print(f"ошибка: не удалось занять порт {args.port}: {e.strerror}", file=sys.stderr)
        return 1
    return 0


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    help_text = "локальный веб-интерфейс"
    p = sub.add_parser(
        "ui",
        parents=[common],
        help=help_text,
        description=(
            "Запускает веб-интерфейс на 127.0.0.1 и открывает его в браузере по ссылке с токеном. "
            "Запуски, начатые в интерфейсе, отменяются при остановке сервера."
        ),
    )
    p.add_argument("--port", type=int, help="порт (по умолчанию случайный свободный)")
    p.add_argument("--no-open", action="store_true", help="не открывать браузер, только показать ссылку")
    p.set_defaults(func=_ui)
