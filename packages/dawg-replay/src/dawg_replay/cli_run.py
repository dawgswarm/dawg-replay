"""Команды CLI запусков: run, runs, logs, cancel. Тонкая обёртка над dawg_replay.launch."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from dawg_replay_kit.events import ConsoleSink

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.cli_store import Handler, run_handler
from dawg_replay.runs import Run

EXIT_RUN_FAILED = 3  # run --follow: запуск закончился статусом failed


def _run_line(r: Run) -> str:
    draft = " черновик" if r.draft else ""
    version = f" v{r.script_version}" if r.script_version is not None else ""
    summary = f"  {r.summary}" if r.summary else ""
    return f"{r.id}  {r.label:<18} {r.script_name}{version}{draft}  {r.created_at}{summary}"


class EventTail:
    """Дочитывает новые строки events.jsonl и печатает их в читаемом виде."""

    def __init__(self, path: Path, out: Any, output_dir: Path):
        self._path = path
        self._out = out
        self._pos = 0
        self._buf = b""
        self._sink = ConsoleSink(out, output_dir)

    def poll(self) -> None:
        try:
            with open(self._path, "rb") as f:
                f.seek(self._pos)
                chunk = f.read()
        except FileNotFoundError:
            return
        if not chunk:
            return
        self._pos += len(chunk)
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            print(format_event_line(raw.decode("utf-8", errors="replace"), self._sink), file=self._out, flush=True)


def format_event_line(line: str, sink: ConsoleSink) -> str:
    try:
        event = json.loads(line)
        if not isinstance(event, dict):
            raise ValueError
        return sink.format(event)
    except (ValueError, KeyError, TypeError):
        return f"[сырая строка] {line}"


# --- команды -----------------------------------------------------------------


def _run(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    prepared = launch.prepare(app, args.target, preset=args.preset, params_file=args.params, sets=args.set)
    run_id = prepared.run.id
    run_dir = app.paths.run_dir(run_id)
    for w in prepared.warnings:
        print(f"предупреждение: {w}", file=sys.stderr)

    if not args.follow:
        pid = launch.spawn_supervisor(app, prepared)
        rec = app.runs.get(run_id)
        data = {"run": rec.to_dict(), "run_dir": str(run_dir), "supervisor_pid": pid, "warnings": prepared.warnings}
        text = (
            f"запуск {run_id} идёт в фоне ({rec.script_name})\n"
            f"  ход: dawg-replay logs {run_id} --events\n"
            f"  отменить: dawg-replay cancel {run_id}"
        )
        return data, text

    interrupted = threading.Event()
    previous = {}

    def on_signal(signum, frame):
        if not interrupted.is_set():
            print("\nотмена запуска...", file=sys.stderr, flush=True)
        interrupted.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            previous[sig] = signal.signal(sig, on_signal)
    tail = None if args.json else EventTail(run_dir / "events.jsonl", sys.stdout, run_dir / "output")
    if tail is not None:
        print(f"запуск {run_id} ({prepared.run.script_name}), Ctrl+C отменяет", flush=True)
    try:
        rec = launch.supervise(
            app, run_id, prepared.payload(), interrupted=interrupted, on_tick=tail.poll if tail else None
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if tail is not None:
        tail.poll()
    args._exit_code = EXIT_RUN_FAILED if rec.status == "failed" else 0
    lines = [f"итог: {rec.label}" + (f", {rec.summary}" if rec.summary else "")]
    if rec.exit_code not in (0, None):
        lines.append(f"код выхода {rec.exit_code}; stderr: dawg-replay logs {run_id}")
    if rec.error:
        lines.append(rec.error)
    return {"run": rec.to_dict(), "run_dir": str(run_dir), "warnings": prepared.warnings}, "\n".join(lines)


def _runs(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    launch.recover_lost(app)
    from dawg_replay import cli_release

    runs = app.runs.list(args.name, args.limit)
    out, lines = [], []
    for r in runs:
        suspects = cli_release.suspects_for(app, r.id)
        out.append({**r.to_dict(), "suspects": suspects})
        line = _run_line(r)
        if suspects:
            line += f"\n    возможно, остался браузер ({len(suspects)}): dawg-replay logs {r.id}"
        lines.append(line)
    return {"runs": out}, "\n".join(lines) or "запусков нет"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _logs(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    launch.recover_lost(app)
    from dawg_replay import cli_release

    rec = app.runs.get(args.run_id)
    run_dir = app.paths.run_dir(rec.id)
    suspects = cli_release.suspects_for(app, rec.id)
    warning = "\n".join(cli_release.warning_lines(app, rec.id))
    if args.events:
        lines = _read(run_dir / "events.jsonl").splitlines()
        events: list[Any] = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except ValueError:
                events.append({"t": "raw", "line": line})
        sink = ConsoleSink(None, run_dir / "output")
        text = "\n".join(format_event_line(line, sink) for line in lines) or "событий нет"
        if warning:
            text += "\n\n" + warning
        return {"run": rec.to_dict(), "events": events, "suspects": suspects}, text
    stdout, stderr = _read(run_dir / "stdout.log"), _read(run_dir / "stderr.log")
    parts = [f"запуск {rec.id}: {rec.label}"]
    if warning:
        parts.append(warning)
    parts += ["--- stdout ---", stdout.rstrip("\n")] if stdout else ["--- stdout пуст ---"]
    parts += ["--- stderr ---", stderr.rstrip("\n")] if stderr else ["--- stderr пуст ---"]
    return {"run": rec.to_dict(), "stdout": stdout, "stderr": stderr, "suspects": suspects}, "\n".join(parts)


def _cancel(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    # Сначала отмена: если супервизора нет, скрипт получит мягкий сигнал раньше, чем recover_lost убьёт его дерево.
    rec = launch.cancel(app, args.run_id, wait=not args.no_wait)
    launch.recover_lost(app)
    if rec.running:
        text = f"отмена запуска {rec.id} запрошена, он ещё завершается"
    else:
        text = f"запуск {rec.id}: {rec.label}"
    return {"run": rec.to_dict()}, text


# --- регистрация -------------------------------------------------------------


def _handler_cmd(handler: Handler):
    def run(args: argparse.Namespace) -> int:
        code = run_handler(handler, args)
        return getattr(args, "_exit_code", 0) if code == 0 else code

    return run


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    def add(name: str, handler: Handler, help: str, description: str | None = None) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help, description=description or help)
        p.set_defaults(func=_handler_cmd(handler))
        return p

    p = add(
        "run",
        _run,
        "запустить скрипт",
        "Запускает зарегистрированный скрипт по имени или файл script.py (черновой запуск). "
        "Значения: default < --preset < --params < --set. Секреты берутся из хранилища паролей. "
        "Без --follow запуск идёт в фоне и переживает закрытие терминала.",
    )
    p.add_argument("target", metavar="name|file", help="имя скрипта или путь к script.py")
    p.add_argument("--params", metavar="FILE", help="JSON со значениями")
    p.add_argument("--set", action="append", metavar="K=V", help="значение параметра, можно повторять")
    p.add_argument("--preset", help="пресет скрипта")
    p.add_argument("--follow", action="store_true", help="показывать события и ждать конца; Ctrl+C отменяет")

    p = add("runs", _runs, "история запусков")
    p.add_argument("name", nargs="?", help="только запуски этого скрипта")
    p.add_argument("--limit", type=int, default=20, help="сколько последних показать (20)")

    p = add("logs", _logs, "сырой stdout и stderr запуска или его события")
    p.add_argument("run_id")
    p.add_argument("--events", action="store_true", help="события из events.jsonl вместо сырого лога")

    p = add("cancel", _cancel, "отменить запуск: мягкий сигнал, через grace секунд kill дерева процессов")
    p.add_argument("run_id")
    p.add_argument("--no-wait", action="store_true", help="не ждать завершения")
