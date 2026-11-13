"""Запуск одного скрипта отдельным процессом (ТЗ 5.3, 5.4, 5.7, 6.6).

Модуль ничего не знает о БД и CLI. Вызывающий код готовит проверенные значения
параметров и id запуска, а получает итог `RunOutcome`.

    run = ScriptRun(script, run_id="r1", runs_dir=data / "runs", values={...},
                    secret_names={"sheet_token"}, api_key=key, timeout=3600)
    run.start()
    ...
    run.cancel()          # из любого потока, не блокирует
    outcome = run.wait()

Каталог запуска `runs/<run_id>/`:

    params.json      значения параметров без секретов
    events.jsonl     события скрипта (DAWG_SCRIPT_EVENTS)
    stdout.log       сырой stdout процесса
    stderr.log       сырой stderr процесса
    output/          DAWG_SCRIPT_OUTPUT_DIR

Секреты. `params.json` в каталоге запуска лежит без секретов (ТЗ 6.2). Если у
запуска есть секреты, скрипт получает в `DAWG_SCRIPT_PARAMS` отдельный файл
с подставленными значениями: он создаётся в личном временном каталоге (0700),
сам файл 0600, и удаляется сразу после завершения процесса. Удалять его сразу
после старта нельзя: `uv run` сначала ставит зависимости (это может занять
минуты), kit читает параметры только потом, при первом обращении. Класть файл
в каталог запуска тоже нельзя: каталог живёт в истории, попадает в бэкапы
и показывается пользователю.

Отмена и таймаут (5.7). Процесс стартует в своей группе: на POSIX новой сессией,
на Windows с CREATE_NEW_PROCESS_GROUP. Мягкий сигнал уходит всей группе
(SIGTERM или CTRL_BREAK_EVENT), так его получают и `uv`, и интерпретатор
скрипта. Kit превращает сигнал в SystemExit, скрипт закрывает браузеры
и дописывает события. Через `grace` секунд (15 по умолчанию) дерево
убивается: SIGKILL группе или `taskkill /F /T`. После любого завершения
оставшиеся в группе процессы тоже убиваются, сирот не остаётся.

После того как процесс и его дерево завершились, вызываются хуки `after_exit`.
Через них подключается страховка браузеров (5.6).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GRACE_SECONDS = 15.0
DEFAULT_COMMAND: tuple[str, ...] = ("uv", "run")

ENV_API_KEY = "DAWG_API_KEY"
ENV_BASE_URL = "DAWG_BASE_URL"
ENV_PARAMS = "DAWG_SCRIPT_PARAMS"
ENV_EVENTS = "DAWG_SCRIPT_EVENTS"
ENV_OUTPUT_DIR = "DAWG_SCRIPT_OUTPUT_DIR"
ENV_RUN_ID = "DAWG_SCRIPT_RUN_ID"

# Переменные, которые не наследуются от процесса приложения: их значения задаёт только раннер.
_STRIP_PREFIXES = ("DAWG_SCRIPT_", "DAWG_SECRET_")
_STRIP_NAMES = (ENV_API_KEY, ENV_BASE_URL)

RESULT_STATUSES = ("ok", "partial", "failed")

# Причина, по которой запуск закончился так, как закончился.
REASON_CANCELLED = "cancelled"
REASON_TIMEOUT = "timeout"
REASON_EXIT_CODE = "exit_code"  # ненулевой код выхода без отмены и таймаута

_IS_WINDOWS = sys.platform == "win32"
_POLL = 0.05


class RunnerError(Exception):
    """Запуск не удалось начать."""


@dataclass
class RunOutcome:
    """Итог запуска.

    `status` по правилам 5.4: ненулевой выход всегда `failed`, выход 0 берёт
    статус последнего `result`, без него `ok`. Отмена и таймаут дают `failed`
    с причиной в `reason`.
    """

    run_id: str
    run_dir: Path
    status: str
    reason: str | None
    exit_code: int | None
    result: dict[str, Any] | None
    started_at: str
    finished_at: str
    killed: bool = False  # дерево пришлось убить: мягкий сигнал не помог за grace
    hook_errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str | None:
        return self.result.get("summary") if self.result else None

    @property
    def label(self) -> str:
        """Статус для показа: `ok`, `partial`, `failed`, `failed (timeout)`, `failed (cancelled)`."""
        if self.reason in (REASON_TIMEOUT, REASON_CANCELLED):
            return f"{self.status} ({self.reason})"
        return self.status


AfterExit = Callable[[RunOutcome], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ScriptRun:
    """Один запуск скрипта.

    `values` содержит проверенные значения параметров вместе с секретами,
    `secret_names` перечисляет параметры типа `secret`. `timeout` в секундах,
    None без ограничения. `command` задаёт, чем запускать файл (по умолчанию
    `uv run`), тесты подставляют интерпретатор. `env` дописывается поверх
    окружения процесса приложения. `secrets_dir` задаёт, где создавать временный
    каталог с файлом секретов (по умолчанию системный временный каталог).
    """

    def __init__(
        self,
        script: str | Path,
        run_id: str,
        runs_dir: str | Path,
        values: Mapping[str, Any] | None = None,
        secret_names: Collection[str] = (),
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        grace: float = GRACE_SECONDS,
        command: Sequence[str] = DEFAULT_COMMAND,
        env: Mapping[str, str] | None = None,
        after_exit: Iterable[AfterExit] = (),
        secrets_dir: str | Path | None = None,
    ):
        self.script = Path(script).resolve()
        self.run_id = run_id
        self.run_dir = Path(runs_dir) / run_id
        self.params_path = self.run_dir / "params.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.stdout_path = self.run_dir / "stdout.log"
        self.stderr_path = self.run_dir / "stderr.log"
        self.output_dir = self.run_dir / "output"
        self._values = dict(values or {})
        self._secret_names = set(secret_names)
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._grace = grace
        self._command = list(command)
        self._extra_env = dict(env or {})
        self._after_exit = list(after_exit)
        self._secrets_base = Path(secrets_dir) if secrets_dir is not None else None

        self._proc: subprocess.Popen[bytes] | None = None
        self._secret_dir: Path | None = None
        self._stop = threading.Event()
        self._stop_reason: str | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._outcome: RunOutcome | None = None
        self._started_at = ""

    # --- жизненный цикл ---

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def outcome(self) -> RunOutcome | None:
        return self._outcome

    def start(self) -> None:
        """Готовит каталог запуска и стартует процесс. Ошибку старта бросает как RunnerError."""
        if self._proc is not None:
            raise RunnerError("запуск уже начат")
        if not self.script.is_file():
            raise RunnerError(f"файла скрипта нет: {self.script}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events_path.touch()
        _write_json(self.params_path, {k: v for k, v in self._values.items() if k not in self._secret_names})
        params_for_script = self.params_path
        try:
            if any(self._values.get(n) not in (None, "") for n in self._secret_names):
                params_for_script = self._write_secret_params()
            env = self._environment(params_for_script)
            argv = [*self._command, str(self.script)]
            with open(self.stdout_path, "wb") as out, open(self.stderr_path, "wb") as err:
                try:
                    self._proc = subprocess.Popen(
                        argv,
                        cwd=self.run_dir,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        **_group_kwargs(),
                    )
                except OSError as e:
                    raise RunnerError(f"не удалось запустить {' '.join(argv)}: {e}") from None
        except BaseException:
            self._remove_secret_params()
            raise
        self._started_at = _now()
        self._thread = threading.Thread(target=self._supervise, name=f"dawg-run-{self.run_id}", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Отменить запуск по 5.7. Не блокирует; итог ждите через wait()."""
        self._request_stop(REASON_CANCELLED)

    def wait(self, timeout: float | None = None) -> RunOutcome | None:
        """Ждёт конца запуска вместе с хуками. С timeout может вернуть None, если запуск ещё идёт."""
        if self._thread is None:
            raise RunnerError("запуск не начат")
        self._thread.join(timeout)
        return self._outcome

    def run(self) -> RunOutcome:
        """start() и wait() одним вызовом."""
        self.start()
        outcome = self.wait()
        assert outcome is not None
        return outcome

    # --- внутреннее ---

    def _request_stop(self, reason: str) -> None:
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
        self._stop.set()

    def _environment(self, params_path: Path) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(_STRIP_PREFIXES) and k not in _STRIP_NAMES
        }
        env.update(
            {
                ENV_PARAMS: str(params_path),
                ENV_EVENTS: str(self.events_path.resolve()),
                ENV_OUTPUT_DIR: str(self.output_dir.resolve()),
                ENV_RUN_ID: self.run_id,
                # Сырые логи пишутся сразу, а не при выходе; кодировка не зависит от локали Windows.
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        if self._api_key:
            env[ENV_API_KEY] = self._api_key
        if self._base_url:
            env[ENV_BASE_URL] = self._base_url
        env.update(self._extra_env)
        return env

    def _write_secret_params(self) -> Path:
        if self._secrets_base is not None:
            self._secrets_base.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdtemp создаёт каталог 0700. Имя начинается с run_id: по нему приложение
        # находит и удаляет каталоги, оставшиеся после падения.
        self._secret_dir = Path(
            tempfile.mkdtemp(prefix=f"{self.run_id}-" if self._secrets_base else f"dawg-replay-{self.run_id}-",
                             dir=self._secrets_base)
        )
        path = self._secret_dir / "params.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._values, f, ensure_ascii=False)
        return path

    def _remove_secret_params(self) -> None:
        if self._secret_dir is not None:
            shutil.rmtree(self._secret_dir, ignore_errors=True)
            self._secret_dir = None

    def _supervise(self) -> None:
        proc = self._proc
        assert proc is not None
        deadline = time.monotonic() + self._timeout if self._timeout is not None else None
        killed = False
        try:
            while proc.poll() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    self._request_stop(REASON_TIMEOUT)
                if self._stop.wait(_POLL):
                    killed = terminate(proc, self._grace)
                    break
            proc.wait()
            # Процессы, которые скрипт оставил в группе, не переживают запуск.
            kill_tree(proc.pid, leader_alive=False)
        finally:
            self._remove_secret_params()
        outcome = RunOutcome(
            run_id=self.run_id,
            run_dir=self.run_dir,
            status="failed",
            reason=None,
            exit_code=proc.returncode,
            result=last_result(self.events_path),
            started_at=self._started_at,
            finished_at=_now(),
            killed=killed,
        )
        outcome.status, outcome.reason = decide(proc.returncode, outcome.result, self._stop_reason)
        for hook in self._after_exit:
            try:
                hook(outcome)
            except Exception as e:  # хук не должен ломать итог запуска
                outcome.hook_errors.append(f"{getattr(hook, '__name__', hook)!s}: {type(e).__name__}: {e}")
        self._outcome = outcome


def decide(exit_code: int | None, result: Mapping[str, Any] | None, stop_reason: str | None) -> tuple[str, str | None]:
    """Статус и причина по 5.4 и 5.7."""
    if stop_reason is not None:
        return "failed", stop_reason
    if exit_code != 0:
        return "failed", REASON_EXIT_CODE
    if result is not None:
        return result["status"], None
    return "ok", None


def last_result(events_path: str | Path) -> dict[str, Any] | None:
    """Последнее корректное событие `result` из файла событий. Битые строки пропускаются."""
    found = None
    try:
        with open(events_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("t") == "result"
                    and event.get("status") in RESULT_STATUSES
                ):
                    found = event
    except FileNotFoundError:
        return None
    return found


# --- процессы и группы ---


def _group_kwargs() -> dict[str, Any]:
    if _IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def soft_stop(pid: int) -> bool:
    """Мягкий сигнал группе процесса `pid`. False, если группы уже нет.

    На Windows CTRL_BREAK_EVENT доходит, только если у приложения и скрипта
    общая консоль.
    """
    try:
        if _IS_WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def kill_tree(pid: int, leader_alive: bool = True) -> None:
    """Убивает дерево процесса `pid` без предупреждения.

    На POSIX это SIGKILL группе (раннер запускает процесс лидером своей
    сессии). На Windows `taskkill /F /T`, ему нужен ещё живой корень дерева,
    поэтому при `leader_alive=False` на Windows ничего не делается.
    """
    if _IS_WINDOWS:
        if leader_alive:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def terminate(proc: subprocess.Popen[Any], grace: float = GRACE_SECONDS) -> bool:
    """Процедура 5.7: мягкий сигнал, ожидание `grace` секунд, затем kill дерева.

    Возвращает True, если дерево пришлось убить.
    """
    if proc.poll() is not None:
        return False
    soft_stop(proc.pid)
    try:
        proc.wait(grace)
        return False
    except subprocess.TimeoutExpired:
        pass
    kill_tree(proc.pid, leader_alive=True)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return True


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
