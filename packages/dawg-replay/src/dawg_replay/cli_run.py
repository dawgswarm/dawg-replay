"""Команды CLI запусков: run, runs, logs, fix-prompt, cancel, answer. Тонкая обёртка над dawg_replay.launch."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from dawg_replay_kit.events import ConsoleSink

from dawg_replay import asks, diagnosis, launch, repair
from dawg_replay.app import App
from dawg_replay.cli_store import Handler, add_row_argument, row_values, run_handler
from dawg_replay.errors import ReplayError
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


class TerminalAnswers:
    """`run --follow`: вопрос скрипта (kit.ask) печатается, ответ читается из терминала.

    Ввод идёт в отдельном потоке: супервизор в главном потоке не должен ждать
    человека (таймаут, отмена по Ctrl+C). Ответ пишется тем же `asks.write_answer`,
    что и из UI; если на вопрос уже ответили в UI, строка из терминала идёт на
    следующий вопрос или отбрасывается.
    """

    def __init__(self, run_dir: Path, out: Any, read_line: Any = None, read_secret: Any = None):
        import getpass

        self._run_dir = run_dir
        self._out = out
        self._read_line = read_line or (lambda: sys.stdin.readline())
        self._read_secret = read_secret or (lambda: getpass.getpass("ответ (не отображается): ", stream=out))
        self._asked: set[str] = set()
        self._reader: threading.Thread | None = None

    def poll(self) -> None:
        if self._reader is not None and self._reader.is_alive():
            return
        for q in asks.waiting(self._run_dir):
            if q.id in self._asked:
                continue
            self._asked.add(q.id)
            self._print(q)
            self._reader = threading.Thread(target=self._read, args=(q,), name="dawg-answer-input", daemon=True)
            self._reader.start()
            return

    def _print(self, q: asks.Question) -> None:
        print(f"\nскрипт спрашивает: {q.question}", file=self._out, flush=True)
        if q.kind == "confirm":
            print("ответьте yes или no и нажмите Enter", file=self._out, flush=True)
        elif q.kind == "choice":
            assert q.choices is not None
            for i, c in enumerate(q.choices, 1):
                print(f"  {i}. {c}", file=self._out, flush=True)
            print("номер или текст варианта и Enter", file=self._out, flush=True)
        elif not q.secret:
            print("ответ и Enter", file=self._out, flush=True)

    def _read(self, q: asks.Question) -> None:
        while True:
            try:
                raw = self._read_secret() if q.secret else self._read_line()
            except (EOFError, OSError):
                return
            if raw == "":
                return  # ввод закрыт
            raw = raw.rstrip("\r\n")
            if not asks.is_waiting(self._run_dir, q):
                print("на этот вопрос уже ответили или срок вышел", file=self._out, flush=True)
                return
            try:
                asks.write_answer(self._run_dir, q, asks.normalize(q, raw))
            except ReplayError as e:
                if e.code == "bad_answer":
                    print(f"{e.message}; попробуйте ещё раз", file=self._out, flush=True)
                    continue
                print(e.message, file=self._out, flush=True)
                return
            print("ответ отправлен", file=self._out, flush=True)
            return


# --- команды -----------------------------------------------------------------


def _wait_printer(args: argparse.Namespace):
    """Строки об ожидании слота: в терминал или, у планового запуска, в лог расписания."""
    out = sys.stderr if args.json else sys.stdout

    def on_wait(info: dict[str, Any]) -> None:
        clock = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if info["state"] == "started":
            if info["waited_seconds"] >= 1:
                print(f"[{clock}] слот освободился, ждали {_minutes(info['waited_seconds'])}", file=out, flush=True)
            return
        ahead = f", впереди в очереди {info['ahead']}" if info["ahead"] else ""
        print(
            f"[{clock}] лимит занят: идут {info['running']} из {info['limit']}{ahead}. "
            f"Ждём слот, не дольше {_minutes(info['left_seconds'], up=True)}",
            file=out,
            flush=True,
        )

    return on_wait


def _minutes(seconds: float, up: bool = False) -> str:
    m = int(-(-seconds // 60)) if up else int(seconds // 60)
    return f"{m} мин" if m else f"{int(seconds)} с"


def _run(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    try:
        rows = row_values(app, args.target, args.row, args.set)
        prepared = launch.prepare(
            app, args.target, preset=args.preset, params_file=args.params, sets=args.set, values=rows or None,
            schedule_id=args.schedule, wait=args.wait, on_wait=_wait_printer(args), notify=args.notify,
        )
    except ReplayError as e:
        if args.schedule and e.code != "run_skipped":
            launch.record_not_started(app, args.schedule, args.target, args.preset, e)
        if e.code != "run_skipped":
            raise
        # Не дождались слота: запуск записан в историю как пропущенный.
        args._exit_code = EXIT_RUN_FAILED
        rec = app.runs.get(e.details["run"])
        return {"run": rec.to_dict(), "skipped": True}, f"запуск {rec.id} {e.message}"
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
    # Вопросы скрипта принимаем из терминала, только если он есть: в --json и из планировщика отвечают через answer или UI.
    answers = TerminalAnswers(run_dir, sys.stdout) if tail is not None and _interactive() else None
    if tail is not None:
        print(f"запуск {run_id} ({prepared.run.script_name}), Ctrl+C отменяет", flush=True)

    def on_tick() -> None:
        if tail is not None:
            tail.poll()
        if answers is not None:
            answers.poll()

    try:
        rec = launch.supervise(
            app, run_id, prepared.payload(), interrupted=interrupted, on_tick=on_tick if tail else None
        )
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if tail is not None:
        tail.poll()
    args._exit_code = EXIT_RUN_FAILED if rec.status == "failed" else 0
    lines = [f"итог: {rec.label}" + (f", {rec.summary}" if rec.summary else "")]
    dg = diagnosis.diagnose(app, rec)
    if dg and dg.get("ask_timeout"):
        # Не поломка скрипта: на вопрос не ответили. Как на экране запуска, без трейсбека.
        lines.append(f"{dg['summary']} Скрипт ждал до срока и завершился. Сам скрипт исправен: "
                     "запустите его снова и ответьте на вопрос (здесь же, в интерфейсе или dawg-replay answer).")
    elif rec.exit_code not in (0, None):
        lines.append(f"код выхода {rec.exit_code}; stderr: dawg-replay logs {run_id}")
    if rec.error:
        lines.append(rec.error)
    if dg and not dg.get("ask_timeout"):
        lines.append("скрипт ничего не сообщил; последние строки stderr:")
        lines += [f"  {line}" for line in dg["tail"]]
        if dg["hint"]:
            lines.append(dg["hint"])
    data = {"run": rec.to_dict(), "run_dir": str(run_dir), "warnings": prepared.warnings, "diagnosis": dg}
    return data, "\n".join(lines)


def _interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def _answer(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    raw = args.answer
    if raw == "-":
        if _interactive():
            import getpass

            raw = getpass.getpass("ответ (не отображается): ")
        else:
            raw = sys.stdin.readline().rstrip("\r\n")
    q, value = asks.answer(app, args.run_id, raw, args.id)
    shown = "скрыт" if q.secret else asks.answer_text(value)
    data = {"run": args.run_id, "question": {k: v for k, v in q.to_dict().items() if k != "answer"},
            "accepted": None if q.secret else value}
    return data, f"ответ на вопрос «{q.question}» отправлен ({shown}), скрипт продолжит работу"


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
    # Логи читает и агент, который чинит скрипт: ключ DAWG и сохранённые секреты скрипта
    # маскируются так же, как в хвосте stderr на экране запуска.
    secrets = diagnosis.known_secrets(app, rec.script_name)
    if args.events:
        lines = diagnosis.mask(_read(run_dir / "events.jsonl"), secrets).splitlines()
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
    stdout = diagnosis.mask(_read(run_dir / "stdout.log"), secrets)
    stderr = diagnosis.mask(_read(run_dir / "stderr.log"), secrets)
    parts = [f"запуск {rec.id}: {rec.label}"]
    if warning:
        parts.append(warning)
    parts += ["--- stdout ---", stdout.rstrip("\n")] if stdout else ["--- stdout пуст ---"]
    parts += ["--- stderr ---", stderr.rstrip("\n")] if stderr else ["--- stderr пуст ---"]
    hint = diagnosis.hint_for(stderr) if rec.status == "failed" else None
    if hint:
        parts += ["", hint]
    data = {"run": rec.to_dict(), "stdout": stdout, "stderr": stderr, "suspects": suspects, "hint": hint}
    return data, "\n".join(parts)


def _fix_prompt(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    launch.recover_lost(app)
    rec = app.runs.get(args.run_id)
    data = repair.fix_prompt(app, rec)
    return {"run": rec.to_dict(), **{k: v for k, v in data.items() if k != "text"}, "prompt": data["text"]}, data["text"]


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
        "Значения: default < --preset < --params < --set и --row. Секреты берутся из хранилища паролей. "
        "Таблицу задают файлом (--set имя=@файл.xlsx) или строками (--row имя колонка=значение …). "
        "Без --follow запуск идёт в фоне и переживает закрытие терминала.",
    )
    p.add_argument("target", metavar="name|file", help="имя скрипта или путь к script.py")
    p.add_argument("--params", metavar="FILE", help="JSON со значениями")
    p.add_argument("--set", "--param", dest="set", action="append", metavar="K=V",
                   help="значение параметра, можно повторять; таблица из файла: имя=@файл.xlsx")
    add_row_argument(p)
    p.add_argument("--preset", help="пресет скрипта")
    p.add_argument("--follow", action="store_true", help="показывать события и ждать конца; Ctrl+C отменяет")
    p.add_argument("--schedule", metavar="ID", help="запуск по расписанию ID (так вызывает задача планировщика); ждёт слота, как --wait")
    p.add_argument("--wait", action="store_true",
                   help="если лимит параллельных запусков занят, ждать слота (до wait_slot_minutes, по умолчанию 30 мин)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--notify", dest="notify", action="store_const", const="always",
                   help="написать в Telegram, когда запуск закончится, даже если по настройкам не нужно")
    g.add_argument("--no-notify", dest="notify", action="store_const", const="never",
                   help="не писать в Telegram об успешном итоге этого запуска (о падении пишет по настройкам)")

    p = add("runs", _runs, "история запусков")
    p.add_argument("name", nargs="?", help="только запуски этого скрипта")
    p.add_argument("--limit", type=int, default=20, help="сколько последних показать (20)")

    p = add("logs", _logs, "сырой stdout и stderr запуска или его события")
    p.add_argument("run_id")
    p.add_argument("--events", action="store_true", help="события из events.jsonl вместо сырого лога")

    p = add(
        "fix-prompt",
        _fix_prompt,
        "текст для агента: починить скрипт по упавшему запуску",
        "Печатает готовый промпт для Claude Code или Codex: скрипт, версия, путь, параметры без секретов, "
        "причина, хвост stderr. Ключ DAWG, секреты и токены в адресах скрыты. "
        "Скопировать на macOS: dawg-replay fix-prompt <run_id> | pbcopy",
    )
    p.add_argument("run_id")

    p = add(
        "answer",
        _answer,
        "ответить на вопрос, который задал идущий запуск",
        "Отвечает на вопрос скрипта (kit.ask). Для вопроса да или нет: yes или no; для выбора: номер или текст "
        "варианта. Вопрос виден в dawg-replay logs <run_id> --events и на экране запуска. Секретный ответ "
        "(код из SMS) не попадает ни в историю, ни в логи; чтобы он не остался в истории оболочки, "
        "передайте - и введите его без отображения.",
    )
    p.add_argument("run_id")
    p.add_argument("answer", help="ответ; - чтобы ввести без отображения")
    p.add_argument("--id", metavar="ASK_ID", help="id вопроса, если запуск ждёт нескольких ответов")

    p = add("cancel", _cancel, "отменить запуск: мягкий сигнал, через grace секунд kill дерева процессов")
    p.add_argument("run_id")
    p.add_argument("--no-wait", action="store_true", help="не ждать завершения")
