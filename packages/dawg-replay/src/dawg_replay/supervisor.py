"""Процесс-супервизор фонового запуска: `python -m dawg_replay.supervisor --home <dir> <run_id>`.

Порождается `dawg-replay run` без `--follow` (см. dawg_replay.launch). Секреты,
ключ и команду запуска читает из stdin одним JSON. SIGTERM, SIGINT и SIGBREAK
(на Windows) отменяют запуск по 5.7, а не убивают супервизор.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from dawg_replay.app import App
from dawg_replay.launch import supervise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dawg_replay.supervisor")
    parser.add_argument("--home", required=True)
    parser.add_argument("run_id")
    args = parser.parse_args(argv)

    payload = json.loads(sys.stdin.read() or "{}")
    sys.stdin.close()

    interrupted = threading.Event()

    def on_signal(signum, frame):
        interrupted.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    with App(Path(args.home)) as app:
        run = supervise(app, args.run_id, payload, interrupted=interrupted)
    print(f"запуск {run.id}: {run.label}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
