"""Запуск скриптов поверх раннера и хранилища (ТЗ 5.3, 5.7, 6.3, 6.5, 6.6).

Каждым запуском управляет супервизор: цикл, который держит `ScriptRun`, раз
в несколько секунд пишет heartbeat в БД, проверяет просьбу об отмене и в конце
записывает итог.

- `run --follow`: супервизор работает внутри CLI, запуск живёт дочерним
  процессом CLI. Ctrl+C отменяет запуск по 5.7.
- `run` без `--follow`: CLI порождает отдельный процесс-супервизор
  (`python -m dawg_replay.supervisor`), отвязанный от терминала (на POSIX
  новая сессия, на Windows своя скрытая консоль и своя группа процессов),
  и сразу возвращает id запуска. Секреты и ключ супервизор получает через
  stdin, на диск они не пишутся (кроме временного файла параметров раннера).

Отмена из другого процесса (`dawg-replay cancel`) не шлёт сигналов сама:
она ставит в БД отметку `cancel_requested_at`, супервизор её видит
и выполняет процедуру 5.7. Так отмена одинаково работает на всех ОС,
включая Windows, где CTRL_BREAK доходит только до процессов с общей
консолью. Если супервизора уже нет, отмена бьёт по группе процесса сама.

Потерянные запуски: статус `running`, heartbeat давно не обновлялся
и процесса-супервизора нет. Они получают `failed (lost)`, для них
вызываются хуки страховки, оставшиеся каталоги с секретами удаляются.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay_kit.manifest import Manifest, ManifestError, load_manifest
from dawg_replay_kit.parameters import ParamsError, resolve

from dawg_replay import hooks
from dawg_replay.errors import ReplayError
from dawg_replay.registry import invalid_manifest
from dawg_replay.runner import (
    DEFAULT_COMMAND,
    RunnerError,
    RunOutcome,
    ScriptRun,
    kill_tree,
    last_result,
    soft_stop,
)
from dawg_replay.runs import REASON_LOST, REASON_START_ERROR, Run, new_run_id, parse_ts
from dawg_replay.values import parse_set_args, read_params_file

if TYPE_CHECKING:
    from dawg_replay.app import App

HEARTBEAT_SECONDS = 5.0
STALE_SECONDS = 30.0  # heartbeat старше этого и нет супервизора: запуск потерян
POLL_SECONDS = 0.25

# Чем запускать script.py. По умолчанию `uv run`; тесты и разработка подставляют
# интерпретатор с установленным kit: DAWG_REPLAY_SCRIPT_COMMAND='["python"]'.
ENV_SCRIPT_COMMAND = "DAWG_REPLAY_SCRIPT_COMMAND"

_IS_WINDOWS = sys.platform == "win32"


def script_command() -> list[str]:
    raw = os.environ.get(ENV_SCRIPT_COMMAND)
    if not raw:
        return list(DEFAULT_COMMAND)
    try:
        cmd = json.loads(raw)
    except json.JSONDecodeError:
        cmd = None
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise ReplayError("bad_script_command", f"{ENV_SCRIPT_COMMAND} должен быть JSON-списком строк, получено {raw!r}")
    return cmd


# --- подготовка ----------------------------------------------------------------


@dataclass
class Prepared:
    """Запись запуска создана, процесса ещё нет. Секреты только в памяти."""

    run: Run
    values: dict[str, Any]
    secret_names: list[str]
    api_key: str | None
    base_url: str | None
    command: list[str]
    warnings: list[str] = field(default_factory=list)

    def payload(self) -> dict[str, Any]:
        return {
            "values": self.values,
            "secret_names": self.secret_names,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "command": self.command,
        }


def _target(app: App, target: str) -> tuple[Manifest, Path, bool, int | None]:
    """`name` зарегистрированного скрипта или путь к файлу (черновой запуск)."""
    path = Path(target)
    looks_like_file = path.suffix == ".py" or os.sep in target or "/" in target or path.is_file()
    if looks_like_file:
        if not path.is_file():
            raise ReplayError("file_not_found", f"файл не найден: {path}")
        try:
            manifest = load_manifest(path)
        except ManifestError as e:
            raise invalid_manifest(e, path) from None
        return manifest, path.resolve(), True, None
    version = app.registry.version(target)
    return app.registry.manifest(target), version.path, False, version.version


def merge_values(
    app: App,
    manifest: Manifest,
    preset: str | None,
    params_file: str | None,
    sets: Sequence[str] | None,
) -> dict[str, Any]:
    """default < пресет < --params < --set. Секреты сюда не принимаются."""
    values: dict[str, Any] = {}
    if preset:
        p = app.presets.get(manifest.name, preset, manifest)
        values.update(p.params)
    if params_file:
        values.update(read_params_file(params_file))
    if sets:
        values.update(parse_set_args(manifest, sets))
    secrets_given = [p.name for p in manifest.params if p.type == "secret" and p.name in values]
    if secrets_given:
        problems = [
            f"`{n}`: секрет не передаётся через --params, --set и пресеты, задайте его: "
            f"dawg-replay secret set {manifest.name} {n}"
            for n in secrets_given
        ]
        raise ReplayError("bad_params", "значения параметров не подходят:\n" + "\n".join(f"  {x}" for x in problems), problems)
    return values


def prepare(
    app: App,
    target: str,
    *,
    preset: str | None = None,
    params_file: str | None = None,
    sets: Sequence[str] | None = None,
) -> Prepared:
    recover_lost(app)
    manifest, script_path, draft, version = _target(app, target)
    values = merge_values(app, manifest, preset, params_file, sets)

    secret_names = [p.name for p in manifest.params if p.type == "secret"]
    missing: list[str] = []
    for p in manifest.params:
        if p.type != "secret":
            continue
        value = app.secrets.get_secret(manifest.name, p.name)
        if value is not None:
            values[p.name] = value
        elif p.required:
            missing.append(f"`{p.name}`: обязательный секрет «{p.title}» не задан: dawg-replay secret set {manifest.name} {p.name}")
    try:
        params = resolve(manifest, values, "приложение")
    except ParamsError as e:
        problems = [str(e)] if not missing else [str(e), *missing]
        raise ReplayError("bad_params", "\n".join(problems)) from None
    if missing:
        raise ReplayError("bad_params", "параметры не подходят:\n" + "\n".join(f"  {x}" for x in missing), missing)

    cfg = app.config
    warnings: list[str] = []
    api_key = app.secrets.get_api_key()
    if api_key is None:
        warnings.append("ключ DAWG не задан (dawg-replay config set-key): браузеры DAWG в этом запуске недоступны")
    timeout_minutes = min(manifest.timeout_minutes, cfg.max_timeout_minutes)

    run_id = new_run_id()
    run_dir = app.paths.run_dir(run_id)
    if draft:
        # Черновик копируется: в истории остаётся ровно то, что запускалось.
        run_dir.mkdir(parents=True)
        dest = run_dir / "script.py"
        shutil.copyfile(script_path, dest)
        script_path = dest
    full = params.to_dict(mask_secrets=False)
    try:
        run = app.runs.reserve(
            run_id=run_id,
            script_name=manifest.name,
            script_version=version,
            script_path=str(script_path),
            draft=draft,
            preset=preset,
            params={k: v for k, v in full.items() if k not in secret_names},
            timeout_seconds=timeout_minutes * 60,
            limit=cfg.max_parallel_runs,
        )
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return Prepared(
        run=run,
        values=full,
        secret_names=secret_names,
        api_key=api_key,
        base_url=cfg.base_url or None,
        command=script_command(),
        warnings=warnings,
    )


# --- супервизор ----------------------------------------------------------------


def supervise(
    app: App,
    run_id: str,
    payload: Mapping[str, Any],
    *,
    interrupted: threading.Event | None = None,
    on_tick: Callable[[], None] | None = None,
) -> Run:
    """Ведёт запуск до конца и записывает итог. `interrupted` — отмена изнутри (Ctrl+C, SIGTERM)."""
    store = app.runs
    rec = store.get(run_id)
    store.set_supervisor(run_id, os.getpid())
    cfg = app.config
    run = ScriptRun(
        rec.script_path,
        run_id,
        app.paths.runs,
        values=payload.get("values") or {},
        secret_names=payload.get("secret_names") or (),
        api_key=payload.get("api_key"),
        base_url=payload.get("base_url"),
        timeout=rec.timeout_seconds,
        grace=cfg.cancel_grace_seconds,
        command=payload.get("command") or DEFAULT_COMMAND,
        after_exit=hooks.after_exit_hooks(),
        secrets_dir=app.paths.secrets_tmp,
    )
    try:
        run.start()
    except RunnerError as e:
        store.finish(run_id, status="failed", reason=REASON_START_ERROR, error=str(e))
        _append(run.stderr_path, f"dawg-replay: {e}\n")
        return store.get(run_id)
    store.set_started(run_id, run.pid)

    last_beat = time.monotonic()
    cancelling = False
    outcome: RunOutcome | None = None
    while outcome is None:
        outcome = run.wait(POLL_SECONDS)
        if on_tick is not None:
            on_tick()
        if outcome is not None:
            break
        if not cancelling and (
            (interrupted is not None and interrupted.is_set()) or store.cancel_requested(run_id)
        ):
            cancelling = True
            run.cancel()
        if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
            store.heartbeat(run_id)
            last_beat = time.monotonic()

    store.finish(
        run_id,
        status=outcome.status,
        reason=outcome.reason,
        exit_code=outcome.exit_code,
        summary=outcome.summary,
        killed=outcome.killed,
        error="\n".join(f"хук после выхода: {e}" for e in outcome.hook_errors) or None,
        finished_at=outcome.finished_at,
    )
    return store.get(run_id)


def _append(path: Path, text: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def spawn_supervisor(app: App, prepared: Prepared) -> int:
    """Фоновый запуск: отдельный процесс-супервизор, отвязанный от CLI. Возвращает его pid."""
    run_id = prepared.run.id
    run_dir = app.paths.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "dawg_replay.supervisor", "--home", str(app.paths.home), run_id]
    kwargs: dict[str, Any]
    if _IS_WINDOWS:
        # Своя скрытая консоль: процесс переживает закрытие окна CLI, а CTRL_BREAK
        # от супервизора доходит до скрипта, который делит с ним эту консоль.
        kwargs = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        }
    else:
        kwargs = {"start_new_session": True}
    try:
        with open(run_dir / "supervisor.log", "ab") as log:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                cwd=run_dir,
                close_fds=True,
                **kwargs,
            )
    except OSError as e:
        app.runs.finish(run_id, status="failed", reason=REASON_START_ERROR, error=f"супервизор не запустился: {e}")
        raise ReplayError("start_failed", f"не удалось запустить супервизор: {e}") from None
    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps(prepared.payload(), ensure_ascii=False).encode("utf-8"))
        proc.stdin.close()
    except OSError:
        pass  # супервизор умер сразу; запуск подберёт recover_lost
    app.runs.set_supervisor(run_id, proc.pid)
    # Долгоживущий родитель (сервер UI) не копит зомби.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


# --- отмена ------------------------------------------------------------------


def cancel(app: App, run_id: str, wait: bool = True, wait_timeout: float | None = None) -> Run:
    """Просит отменить запуск. С `wait` ждёт итога (до grace + 30 секунд)."""
    rec = app.runs.get(run_id)
    if not rec.running:
        raise ReplayError("not_running", f"запуск `{run_id}` уже завершён: {rec.label}", rec.to_dict())
    app.runs.request_cancel(run_id)
    grace = app.config.cancel_grace_seconds
    if _supervisor_gone(rec):
        _cancel_orphan(app, rec, grace)
        return app.runs.get(run_id)
    if not wait:
        return app.runs.get(run_id)
    deadline = time.monotonic() + (wait_timeout if wait_timeout is not None else grace + 30)
    while time.monotonic() < deadline:
        rec = app.runs.get(run_id)
        if not rec.running:
            return rec
        if _supervisor_gone(rec):
            _cancel_orphan(app, rec, grace)
            return app.runs.get(run_id)
        time.sleep(POLL_SECONDS)
    return app.runs.get(run_id)


def _supervisor_gone(rec: Run) -> bool:
    if rec.supervisor_pid is None:
        # Между записью и стартом супервизора проходят доли секунды.
        return _stale(rec, seconds=HEARTBEAT_SECONDS * 2)
    return not pid_alive(rec.supervisor_pid)


def _cancel_orphan(app: App, rec: Run, grace: float) -> None:
    """Супервизора нет: отменяем по группе процесса сами и закрываем запуск."""
    killed = False
    if rec.pid and not _IS_WINDOWS and _group_alive(rec.pid):
        soft_stop(rec.pid)
        end = time.monotonic() + grace
        while time.monotonic() < end and _group_alive(rec.pid):
            time.sleep(POLL_SECONDS)
        if _group_alive(rec.pid):
            kill_tree(rec.pid)
            killed = True
    elif rec.pid and _IS_WINDOWS and pid_alive(rec.pid):
        kill_tree(rec.pid)
        killed = True
    _close_lost(app, rec, reason="cancelled", killed=killed)


# --- потерянные запуски ------------------------------------------------------


def _stale(rec: Run, seconds: float | None = None) -> bool:
    seconds = STALE_SECONDS if seconds is None else seconds
    ts = parse_ts(rec.heartbeat_at) or parse_ts(rec.created_at)
    if ts is None:
        return True
    return (datetime.now(timezone.utc) - ts).total_seconds() > seconds


def recover_lost(app: App) -> list[Run]:
    """Запуски `running` без живого супервизора закрывает как `failed (lost)`. Чистит каталоги секретов."""
    lost = []
    for rec in app.runs.running():
        if pid_alive(rec.supervisor_pid) or not _stale(rec):
            continue
        killed = False
        # Скрипт мог пережить супервизора: его группа не должна остаться сиротой.
        if rec.pid and not _IS_WINDOWS and _group_alive(rec.pid):
            kill_tree(rec.pid)
            killed = True
        if _close_lost(app, rec, reason=REASON_LOST, killed=killed):
            lost.append(app.runs.get(rec.id))
    _sweep_secret_dirs(app)
    return lost


def _close_lost(app: App, rec: Run, reason: str, killed: bool) -> bool:
    run_dir = app.paths.run_dir(rec.id)
    result = last_result(run_dir / "events.jsonl")
    if not app.runs.finish(
        rec.id,
        status="failed",
        reason=reason,
        summary=result.get("summary") if result else None,
        killed=killed,
        error="супервизор запуска пропал" if reason == REASON_LOST else "отменён без супервизора",
    ):
        return False  # итог уже поставил кто-то другой
    _remove_secret_dirs(app, rec.id)
    outcome = RunOutcome(
        run_id=rec.id,
        run_dir=run_dir,
        status="failed",
        reason=reason,
        exit_code=None,
        result=result,
        started_at=rec.started_at or rec.created_at,
        finished_at=app.runs.get(rec.id).finished_at or "",
        killed=killed,
    )
    for hook in hooks.after_exit_hooks():
        try:
            hook(outcome)
        except Exception as e:
            app.runs.add_error(rec.id, f"хук после выхода: {getattr(hook, '__name__', hook)!s}: {type(e).__name__}: {e}")
    return True


def _remove_secret_dirs(app: App, run_id: str) -> None:
    base = app.paths.secrets_tmp
    if base.is_dir():
        for d in base.glob(f"{run_id}-*"):
            shutil.rmtree(d, ignore_errors=True)


def _sweep_secret_dirs(app: App) -> None:
    """Удаляет каталоги секретов, чей запуск уже не идёт."""
    base = app.paths.secrets_tmp
    if not base.is_dir():
        return
    alive = {r.id for r in app.runs.running()}
    for d in base.iterdir():
        run_id = d.name.rsplit("-", 1)[0]
        if run_id not in alive:
            shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)


# --- процессы ----------------------------------------------------------------


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if _IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _win_pid_alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
