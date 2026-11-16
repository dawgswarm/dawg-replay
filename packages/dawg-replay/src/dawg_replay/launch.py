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
консолью. Если супервизора уже нет, отмена сама создаёт файл остановки
и шлёт сигнал группе процесса, а через grace убивает дерево.

Потерянные запуски: статус `running`, heartbeat давно не обновлялся
и процесса-супервизора нет, либо система перезагружалась после создания
запуска. pid переиспользуется, поэтому «процесс жив» везде значит «жив
процесс с этим pid и с тем же временем старта, что записано в БД»
(dawg_replay.procinfo). Чужой процесс не убивается и супервизором не считается. Они получают `failed (lost)`, для них
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
from dawg_replay_kit.relative import is_relative

from dawg_replay import (asks, devkit, hooks, insurance, logins, outputs, procinfo, repair, tg_asks, tg_notify, tg_rules,
                         transfer)
from dawg_replay.errors import ReplayError
from dawg_replay.presets import normalize as normalize_preset
from dawg_replay.registry import invalid_manifest
from dawg_replay.runner import (
    DEFAULT_COMMAND,
    RunnerError,
    STOP_FILE,
    RunOutcome,
    ScriptRun,
    kill_tree,
    last_result,
    soft_stop,
)
from dawg_replay.runs import REASON_LOST, REASON_START_ERROR, WAITER_LOST, Run, WaitPolicy, new_run_id, parse_ts
from dawg_replay.secrets import has_control_chars
from dawg_replay.uploads import Uploads
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


def run_command(app: App) -> tuple[list[str], dict[str, str]]:
    """Команда запуска script.py и добавки к окружению.

    `DAWG_REPLAY_SCRIPT_COMMAND` (тесты) важнее всего. Иначе `uv run`, а с настройкой
    `dev_kit_source` ещё локальная сборка kit (dawg_replay.devkit).
    """
    if os.environ.get(ENV_SCRIPT_COMMAND):
        return script_command(), {}
    source = app.config.dev_kit_source
    if not source:
        return list(DEFAULT_COMMAND), {}
    kit = devkit.ensure(source, app.paths.dev_kit)
    return [*DEFAULT_COMMAND, *kit.uv_args], kit.env


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
    env: dict[str, str] = field(default_factory=dict)
    secret_files: list[str] = field(default_factory=list)  # параметры file с secret: копирует раннер

    def payload(self) -> dict[str, Any]:
        return {
            "values": self.values,
            "secret_names": self.secret_names,
            "secret_files": self.secret_files,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "command": self.command,
            "env": self.env,
        }


def target_manifest(app: App, target: str) -> Manifest:
    """Манифест зарегистрированного скрипта или файла script.py, как его видит prepare."""
    return _target(app, target)[0]


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


def _as_list(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else [value]


def _has_paths(value: Any) -> bool:
    items = _as_list(value)
    return bool(items) and all(isinstance(f, dict) and isinstance(f.get("path"), str) for f in items)


def _safe_name(name: str) -> str:
    """Имя файла для копии: без каталогов и управляющих символов."""
    base = Path(str(name).replace("\\", "/")).name
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*').strip(" .")
    return base[:120] or "file"


def copy_files_into(dest_dir: Path, value: Any) -> Any:
    """Копирует файлы значения параметра `file` в каталог; возвращает значение с новыми путями."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for f in _as_list(value):
        target = dest_dir / _safe_name(f["name"])
        n = 1
        while target.exists():
            n += 1
            target = dest_dir / f"{n}-{_safe_name(f['name'])}"
        shutil.copyfile(f["path"], target)
        out.append({"path": str(target.resolve()), "name": f["name"], "size": f["size"]})
    return out if isinstance(value, list) else out[0]


def _copy_inputs(run_dir: Path, files: Mapping[str, Any]) -> dict[str, Any]:
    return {name: copy_files_into(run_dir / "input" / name, value) for name, value in files.items()}


def merge_values(
    app: App,
    manifest: Manifest,
    preset: str | None,
    params_file: str | None,
    sets: Sequence[str] | None,
    form_values: Mapping[str, Any] | None = None,
    preset_tables: Mapping[str, Any] | None = None,
    explicit: set[str] | None = None,
) -> dict[str, Any]:
    """default < пресет < --params < значения формы UI < --set. Секреты сюда не принимаются.
    `preset_tables`: секретные таблицы пресета (Presets.secret_rows); загруженная в форму или
    через --set таблица их перекрывает, сам пресет при этом не меняется.
    `explicit`: сюда добавляются имена, заданные явно (не пресетом): их передача по связи не трогает."""
    values: dict[str, Any] = {}
    if preset:
        p = app.presets.get(manifest.name, preset, manifest)
        values.update(p.params)
        values.update(preset_tables or {})
        values.update(p.results)
    given: dict[str, Any] = {}
    if params_file:
        given.update(read_params_file(params_file))
    if form_values:
        given.update(form_values)
    if sets:
        given.update(parse_set_args(manifest, sets))
    values.update(given)
    if explicit is not None:
        explicit.update(given)
    secrets_given = [p.name for p in manifest.params if p.type == "secret" and p.name in values]
    if secrets_given:
        problems = [
            f"`{n}`: секрет не передаётся через --params, --set и пресеты, задайте его: "
            f"dawg-replay secret set {manifest.name} {n}"
            for n in secrets_given
        ]
        raise ReplayError("bad_params", "значения параметров не подходят:\n" + "\n".join(f"  {x}" for x in problems), problems)
    return values


# Отказы, после которых плановый запуск не пишется в историю: запись уже есть
# (пропущен или отменён в очереди) или задача сирота (расписания в приложении нет).
_NOT_RECORDED = ("run_skipped", "run_cancelled", "schedule_not_found")


def record_not_started(app: App, schedule_id: str | None, target: str, preset: str | None, error: ReplayError,
                       *, source_run: str | None = None, reason: str | None = None,
                       project_run: str | None = None, project_node: str | None = None) -> Run | None:
    """Плановый запуск или шаг проекта упал до создания запуска: запись `failed (not_started)` в истории
    скрипта, чтобы причина была видна в интерфейсе, а не только в `schedules/<id>.log`.
    Сообщение `failed` в Telegram уходит так же (у шага проекта его заменяет итог проекта)."""
    if error.code in _NOT_RECORDED:
        return None
    try:
        version = app.registry.version(target)
    except ReplayError:
        return None  # скрипта нет: показывать негде
    extra = {"reason": reason} if reason else {}
    rec = app.runs.record_not_started(
        run_id=new_run_id(), script_name=target, script_version=version.version, script_path=str(version.path),
        preset=normalize_preset(preset) or None, schedule_id=schedule_id, error=error.message,
        source_run=source_run, project_run=project_run, project_node=project_node, **extra,
    )
    tg_notify.after_finish(app, rec.id, background=False)
    return rec


def _wait_policy(cfg: Any, on_wait: Callable[[dict[str, Any]], None] | None) -> WaitPolicy:
    pid = os.getpid()
    return WaitPolicy(seconds=cfg.wait_slot_minutes * 60, on_wait=on_wait, pid=pid, pid_started=procinfo.start_time(pid))


def _check_schedule(app: App, schedule_id: str, target: str, preset: str | None) -> None:
    """Задача планировщика без записи в приложении (сирота) не запускает скрипт и не тратит минуты."""
    try:
        s = app.schedules.get(schedule_id)
    except ReplayError:
        raise ReplayError(
            "schedule_not_found",
            f"расписания `{schedule_id}` в приложении нет, а задача в планировщике осталась. "
            f"Снимите её: dawg-replay schedule remove {schedule_id}",
        ) from None
    if (s.script, s.preset) != (target, preset):
        raise ReplayError(
            "schedule_mismatch",
            f"расписание `{schedule_id}` запускает {s.script} с пресетом {s.preset}, а не {target} с {preset}. "
            "Поставьте задачу заново: dawg-replay schedule sync",
        )


def prepare(
    app: App,
    target: str,
    *,
    preset: str | None = None,
    params_file: str | None = None,
    sets: Sequence[str] | None = None,
    values: Mapping[str, Any] | None = None,
    schedule_id: str | None = None,
    wait: bool = False,
    on_wait: Callable[[dict[str, Any]], None] | None = None,
    strict_preset: bool = False,
    notify: str | None = None,
    source_run: str | None = None,
    inputs_of: str | None = None,
    given: Sequence[transfer.Fill] = (),
    unset: Sequence[str] = (),
    project_run: str | None = None,
    project_node: str | None = None,
) -> Prepared:
    """`values`: проверенные значения без секретов (форма UI, dawg_replay.forms).
    `schedule_id`: запуск по расписанию; оно должно быть в приложении и совпадать со скриптом и пресетом.
    `wait`: при занятом лимите ждать слота (`wait_slot_minutes`), а не отказывать. Плановый запуск ждёт всегда.
    `strict_preset`: пресет с значениями, которые текущая версия скрипта не принимает, запуск не начинает
    (`preset_outdated`). Так всегда для планового запуска: без человека нельзя молча подставить `default`,
    а у скриптов действий на сайтах это может изменить не то. Без флага такие значения пропускаются
    с предупреждением в `warnings`.
    `notify`: выбор у запуска для сообщения в Telegram об успешном итоге: None по настройкам, always, never.
    `source_run`: запуск, после которого начат этот (для экрана запуска).
    Ссылки `run:`/`last:` в значениях table/file заменяются файлами (dawg_replay.transfer).
    Ошибка подстановки: ReplayError `input_error`.
    `given`: значения из файлов, собранные заранее (стрелки проекта): поверх пресета и `values`.
    `unset`: параметры без значения, даже если оно есть в пресете (стрелка в них неактивна).
    `project_run`, `project_node`: шаг проекта (dawg_replay.project_runs), запуск помечается ими.
    `inputs_of`: «Запустить снова» и «Перезапустить упавшие»: id исходного запуска, его снимок
    источников переходит к параметрам, которые взяты из него как есть."""
    if notify not in (None, *tg_rules.RUN_CHOICES):
        raise ReplayError("bad_notify", f"notify: always, never или ничего, получено {notify!r}")
    recover_lost(app)
    preset = normalize_preset(preset) or None  # имя пресета хранится без пробелов по краям
    if schedule_id is not None:
        _check_schedule(app, schedule_id, target, preset)
    manifest, script_path, draft, version = _target(app, target)
    outdated = None
    if preset:
        outdated = app.presets.get(manifest.name, preset, manifest).outdated()
    if outdated and (strict_preset or schedule_id is not None):
        raise ReplayError("preset_outdated", outdated[:1].upper() + outdated[1:])
    preset_tables = app.presets.secret_rows(app.presets.get(manifest.name, preset, manifest)) if preset else {}
    preset_files = app.presets.file_values(app.presets.get(manifest.name, preset, manifest), manifest) if preset else {}
    explicit: set[str] = set()
    values = merge_values(app, manifest, preset, params_file, sets, values, {**preset_tables, **preset_files}, explicit)
    for name in unset:
        values.pop(name, None)
    for f in given:
        values[f.param] = f.value
    fills = [*given, *_fill_results(app, manifest, values)]
    inputs = [e for f in fills for e in (f.entry, *f.extra_entries)]
    if inputs_of:
        inputs = _inherit_inputs(app, inputs_of, values, explicit) + inputs
    # Файл без пути (секретный файл из истории для «Запустить снова»): не задан, его дают заново.
    for p in manifest.params:
        if p.type == "file" and p.name in values and not _has_paths(values[p.name]):
            del values[p.name]
    # Секретные таблицы, взятые из пресета: запуск запомнит их для «Перезапустить упавшие».
    from_preset = {k: v for k, v in preset_tables.items() if values.get(k) is v}
    # Даты из выражений (`today-1`): повтор берёт вычисленную дату, экран запуска называет выражение.
    relative_used = {}
    for p in manifest.params:
        given = values.get(p.name) if p.name in values else p.default
        if p.type in ("date", "datetime") and is_relative(given, p.type):
            relative_used[p.name] = given

    # Секретная таблица идёт скрипту как секрет: во временный файл 0700, не в историю.
    secret_names = [p.name for p in manifest.params if p.is_secret]
    missing: list[str] = []
    for p in manifest.params:
        if p.type != "secret":
            continue
        value = app.secrets.get_secret(manifest.name, p.name)
        if value is not None and has_control_chars(value):
            missing.append(f"`{p.name}`: секрет «{p.title}» повреждён, задайте заново: dawg-replay secret set {manifest.name} {p.name}")
        elif value is not None:
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
    warnings: list[str] = [outdated + "; пока взяты значения по умолчанию"] if outdated else []
    api_key = app.secrets.get_api_key()
    if api_key is None:
        warnings.append("ключ DAWG не задан (dawg-replay config set-key): браузеры DAWG в этом запуске недоступны")
    timeout_minutes = min(manifest.timeout_minutes, cfg.max_timeout_minutes)
    command, env = run_command(app)

    run_id = new_run_id()
    run_dir = app.paths.run_dir(run_id)
    if draft:
        # Черновик копируется: в истории остаётся ровно то, что запускалось.
        run_dir.mkdir(parents=True)
        dest = run_dir / "script.py"
        shutil.copyfile(script_path, dest)
        script_path = dest
    full = params.to_dict(mask_secrets=False)
    file_params = [p for p in manifest.params if p.type == "file" and p.name in full]
    history = {k: v for k, v in full.items() if k not in secret_names}
    for p in file_params:
        if p.secret:  # в историю только имена: ни пути, ни содержимого
            history[p.name] = [{"name": f["name"]} for f in _as_list(full[p.name])]
    try:
        run = app.runs.reserve(
            run_id=run_id,
            script_name=manifest.name,
            script_version=version,
            script_path=str(script_path),
            draft=draft,
            preset=preset,
            params=history,
            timeout_seconds=timeout_minutes * 60,
            limit=cfg.max_parallel_runs,
            schedule_id=schedule_id,
            wait=_wait_policy(cfg, on_wait) if (wait or schedule_id) else None,
            notify=notify,
            source_run=source_run,
            project_run=project_run,
            project_node=project_node,
        )
    except ReplayError as e:
        if e.code not in ("run_skipped", "run_cancelled"):  # пропущенный запуск остаётся в истории
            shutil.rmtree(run_dir, ignore_errors=True)
        if e.code == "run_skipped":
            tg_notify.after_finish(app, e.details["run"], background=False)
        raise
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    # Обычный файл копируется в каталог запуска: история и «Запустить снова» ссылаются на копию.
    # Секретный копирует раннер во временный каталог секретов, он удаляется после выхода.
    plain_files = [p for p in file_params if not p.secret]
    if plain_files:
        copied = _copy_inputs(run_dir, {p.name: full[p.name] for p in plain_files})
        full.update(copied)
        app.runs.set_params(run.id, {**history, **copied})
    # Таблица из результата другого запуска: копия файла в каталог входа, рядом с загруженными.
    for f in fills:
        for src in f.table_files:
            copy_files_into(run_dir / "input" / f.param, {"path": str(src), "name": src.name, "size": src.stat().st_size})
    if inputs:
        app.runs.set_inputs(run.id, inputs)
    # Сколько строк в таблицах запуска: секретной таблицы нет в истории, а «2 из 5» нужно и для неё.
    repair.write_table_sizes(run_dir, {p.name: len(full[p.name]) for p in manifest.params
                                       if p.type == "table" and isinstance(full.get(p.name), list)})
    store = Uploads(app.paths.uploads)
    # Секретные таблицы для «Перезапустить упавшие» и «Запустить снова»: копия в загрузках (сутки).
    # Загрузка из формы потом перепривязывается своим id, итог тот же.
    secret_tables = {p.name: full[p.name] for p in manifest.params
                     if p.type == "table" and p.secret and isinstance(full.get(p.name), list)}
    if secret_tables:
        names = app.presets.get(manifest.name, preset, manifest).secret_tables if from_preset else {}
        store.link(run.id, {k: store.put(k, names.get(k, {}).get("file") or "таблица", v, secret=True)
                            for k, v in secret_tables.items()})
    # Секретный файл в истории только именем: для «Запустить снова» запуск помнит его копию
    # в загрузках (0600, сутки), потом файл просят загрузить заново.
    secret_copies = {}
    for p in file_params:
        if p.secret:
            uids = [store.put_file(p.name, f["name"], Path(f["path"]).read_bytes(), secret=True) for f in _as_list(full[p.name])]
            secret_copies[p.name] = uids if p.multiple else uids[0]
    store.link(run.id, secret_copies)
    if relative_used:
        repair.write_relative(run_dir, relative_used)
    return Prepared(
        run=run,
        values=full,
        secret_names=secret_names,
        secret_files=[p.name for p in file_params if p.secret],
        api_key=api_key,
        base_url=cfg.base_url or None,
        command=command,
        warnings=warnings,
        env=env,
    )


def _fill_results(app: App, manifest: Manifest, values: dict[str, Any]) -> list[transfer.Fill]:
    """Подставляет в `values` файлы из результатов других запусков по ссылкам run:/last:.
    Все ошибки сразу, одной ReplayError input_error."""
    problems: list[str] = []
    fills: list[transfer.Fill] = []
    by_name = {p.name: p for p in manifest.params}
    for name, value in list(values.items()):
        p = by_name.get(name)
        ref = transfer.parse_ref(value) if p is not None and p.type in transfer.TRANSFER_TYPES else None
        if ref is None:
            continue  # строка «last:…» в текстовом параметре просто текст
        try:
            fill = transfer.fill_ref(app, p, ref)
        except ReplayError as e:
            problems.append(e.message)
            del values[name]
            continue
        values[name] = fill.value
        fills.append(fill)
    if problems:
        text = problems[0] if len(problems) == 1 else "файлы из результатов не подошли:\n" + "\n".join(f"  {x}" for x in problems)
        raise ReplayError(transfer.INPUT_ERROR, text, problems)
    return fills


def _inherit_inputs(app: App, run_id: str, values: Mapping[str, Any], explicit: set[str]) -> list[dict[str, Any]]:
    """Снимок источников исходного запуска для параметров, переданных повтору из него же:
    файл тот же (копия в каталоге исходного запуска), таблица та же или её часть («Перезапустить упавшие»)."""
    try:
        rec = app.runs.get(run_id)
    except ReplayError:
        return []

    def same(name: str) -> bool:
        now, was = values.get(name), rec.params.get(name)
        if now == was:
            return True
        return isinstance(now, list) and isinstance(was, list) and all(r in was for r in now)

    return [{**e, "from_run": rec.id} for e in rec.inputs if e.get("param") in explicit and same(e["param"])]


def _lost_message(lost: list[str]) -> str:
    """Чего нет для повтора, с согласованием: один файл (он), одна таблица (она), несколько (они)."""
    if len(lost) > 1:
        tail = ("в истории запуска они не хранятся, а копии для повтора живут сутки и уже удалены. "
                "Загрузите их заново")
    elif "файла" in lost[0]:
        tail = "в истории запуска он не хранится, а копия для повтора живёт сутки и уже удалена. Загрузите его заново"
    else:
        tail = "в истории запуска она не хранится, а копия для повтора живёт сутки и уже удалена. Загрузите её заново"
    return f"для повтора нет {', '.join(lost)}: {tail} в форме запуска"


def again_values(app: App, rec: Run) -> dict[str, Any]:
    """Значения для «Запустить снова»: те же, что у запуска. Секретных таблиц и файлов в истории нет,
    их запуск помнит в загрузках сутки; потом повтор не начинается и просит загрузить их заново,
    чтобы скрипт не пошёл молча без файла."""
    manifest = app.registry.manifest(rec.script_name)
    store = Uploads(app.paths.uploads)
    run_dir = app.paths.run_dir(rec.id)
    sizes = repair.read_table_sizes(run_dir)
    values: dict[str, Any] = {}
    lost: list[str] = []
    for p in manifest.params:
        if p.type == "file" and p.secret:
            got = store.linked_files(rec.id, p.name)
            if got is not None:
                values[p.name] = got if p.multiple else got[0]
            elif p.required or rec.params.get(p.name):
                lost.append(f"секретного файла «{p.title}»")
        elif p.type == "table" and p.secret:
            data = store.linked(rec.id, p.name)
            if data is not None:
                values[p.name] = data["rows"]
            elif p.required or p.name in sizes:
                lost.append(f"секретной таблицы «{p.title}»")
        elif p.name in rec.params:
            values[p.name] = rec.params[p.name]
    if lost:
        raise ReplayError(
            "needs_upload",
            _lost_message(lost),
        )
    return values


# --- супервизор ----------------------------------------------------------------


def supervise(
    app: App,
    run_id: str,
    payload: Mapping[str, Any],
    *,
    interrupted: threading.Event | None = None,
    on_tick: Callable[[], None] | None = None,
) -> Run:
    """Ведёт запуск до конца и записывает итог. `interrupted`: отмена изнутри (Ctrl+C, SIGTERM)."""
    store = app.runs
    rec = store.get(run_id)
    store.set_supervisor(run_id, os.getpid(), procinfo.start_time(os.getpid()))
    cfg = app.config
    # Прерывает ожидание страховки (окончание POST) при отмене или выходе приложения.
    # Цикл ниже крутится и пока работают хуки, так что отмена доходит и до них.
    hooks_stop = threading.Event()
    run = ScriptRun(
        rec.script_path,
        run_id,
        app.paths.runs,
        values=payload.get("values") or {},
        secret_names=payload.get("secret_names") or (),
        secret_files=payload.get("secret_files") or (),
        api_key=payload.get("api_key"),
        base_url=payload.get("base_url"),
        timeout=rec.timeout_seconds,
        grace=cfg.cancel_grace_seconds,
        command=payload.get("command") or DEFAULT_COMMAND,
        env=payload.get("env") or None,
        after_exit=[_insurance(app, payload.get("api_key"), stop=hooks_stop), *hooks.after_exit_hooks()],
        secrets_dir=app.paths.secrets_tmp,
        logins_dir=logins.prepare_dir(app.paths, rec.script_name),
    )
    try:
        run.start()
    except RunnerError as e:
        store.finish(run_id, status="failed", reason=REASON_START_ERROR, error=str(e))
        _append(run.stderr_path, f"dawg-replay: {e}\n")
        tg_notify.after_finish(app, run_id)
        _step_finished(app, run_id)
        return store.get(run_id)
    store.set_started(run_id, run.pid, procinfo.start_time(run.pid))
    # Вопросы скрипта в Telegram: сеть в своих потоках, цикл её не ждёт.
    telegram = tg_asks.start(app, store.get(run_id), dict(payload))

    last_beat = time.monotonic()
    cancelling = False
    outcome: RunOutcome | None = None
    while outcome is None:
        outcome = run.wait(POLL_SECONDS)
        if on_tick is not None:
            on_tick()
        if telegram is not None:
            _telegram_errors(app, run.run_dir, telegram.tick())
        if outcome is not None:
            break
        if not cancelling and (
            (interrupted is not None and interrupted.is_set()) or store.cancel_requested(run_id)
        ):
            cancelling = True
            hooks_stop.set()
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
    if telegram is not None:
        _telegram_errors(app, run.run_dir, telegram.close(late=tg_asks.late_sink(app.paths, run.run_dir)))
    # Объявленные файлы результата сверяются до сообщения об итоге: недостающий попадает в него.
    outputs.check_run(app, run_id)
    # Итог и страховка браузеров уже записаны; сообщение об итоге уходит в фоне, запуск его не ждёт.
    tg_notify.after_finish(app, run_id)
    # Шаг проекта: решить следующие узлы (файлы и итог уже на месте).
    _step_finished(app, run_id)
    return store.get(run_id)


def _step_finished(app: App, run_id: str) -> None:
    from dawg_replay import project_runs

    project_runs.on_step_finish(app, run_id)


def _telegram_errors(app: App, run_dir: Path, errors: list[str]) -> None:
    if errors:
        tg_asks.journal(run_dir, errors)
        tg_asks.record_error(app, errors[-1])


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
                # supervisor.log читается как UTF-8; на Windows без этого вывод шёл бы в cp1252.
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                **kwargs,
            )
    except OSError as e:
        app.runs.finish(run_id, status="failed", reason=REASON_START_ERROR, error=f"супервизор не запустился: {e}")
        tg_notify.after_finish(app, run_id, background=False)
        _step_finished(app, run_id)
        raise ReplayError("start_failed", f"не удалось запустить супервизор: {e}") from None
    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps(prepared.payload(), ensure_ascii=False).encode("utf-8"))
        proc.stdin.close()
    except OSError:
        pass  # супервизор умер сразу; запуск подберёт recover_lost
    app.runs.set_supervisor(run_id, proc.pid, procinfo.start_time(proc.pid))
    # Долгоживущий родитель (сервер UI) не копит зомби.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc.pid


# --- отмена ------------------------------------------------------------------


def cancel(app: App, run_id: str, wait: bool = True, wait_timeout: float | None = None) -> Run:
    """Просит отменить запуск. С `wait` ждёт итога (до grace + 30 секунд)."""
    rec = app.runs.get(run_id)
    if rec.waiting and app.runs.cancel_waiting(run_id):
        return app.runs.get(run_id)  # ещё не начался: просто уходит из очереди
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
    """Супервизора запуска точно нет. Чужой процесс с тем же pid супервизором не считается."""
    if _rebooted_since(rec):
        return True
    if rec.supervisor_pid is None or rec.supervisor_started is None:
        # Супервизор ещё не записался (доли секунды после старта) или время его старта
        # узнать не вышло: судим только по heartbeat.
        return _stale(rec, seconds=HEARTBEAT_SECONDS * 2)
    return not procinfo.same_process(rec.supervisor_pid, rec.supervisor_started)


def _cancel_orphan(app: App, rec: Run, grace: float) -> None:
    """Супервизора нет: отменяем по группе процесса сами и закрываем запуск."""
    killed = False
    if _own_group(rec):
        soft_stop(rec.pid, app.paths.run_dir(rec.id) / STOP_FILE)
        # Сигнал получил только корень: ждём его, а не всю группу. Дочерние процессы
        # (драйвер Playwright) живы, пока скрипт убирается, потом их добивает kill_tree.
        end = time.monotonic() + grace
        while time.monotonic() < end and _leader_alive(rec):
            time.sleep(POLL_SECONDS)
        killed = _leader_alive(rec)
        if _own_group(rec):
            kill_tree(rec.pid, leader_alive=_leader_alive(rec))
    if _close_lost(app, rec, reason="cancelled", killed=killed):
        _step_finished(app, rec.id)


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
    for rec in app.runs.waiting():
        if not app.runs.waiter_alive(rec) and app.runs.close_waiting(rec.id, REASON_LOST, WAITER_LOST):
            lost.append(app.runs.get(rec.id))  # процесс, ждавший слота, пропал; запуск не начинался
    for rec in app.runs.running():
        if not _rebooted_since(rec) and not (_supervisor_gone(rec) and _stale(rec)):
            continue
        killed = False
        # Скрипт мог пережить супервизора: его дерево не должно остаться сиротой.
        # Убиваем, только если это точно процессы этого запуска.
        if _own_group(rec):
            kill_tree(rec.pid)
            killed = True
        if _close_lost(app, rec, reason=REASON_LOST, killed=killed):
            lost.append(app.runs.get(rec.id))
    _sweep_secret_dirs(app)
    # Закрыли мы: сообщаем сразу, процесс может быть короткой командой CLI. Потом то, что не ушло раньше.
    for r in lost:
        tg_notify.after_finish(app, r.id, background=False)
        _step_finished(app, r.id)
    tg_notify.retry_pending(app)
    # Запуски проектов, у которых не осталось ведущего процесса (выключен компьютер, процесс упал).
    from dawg_replay import project_runs

    project_runs.recover(app)
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
    asks.drop_secret_answers(run_dir)
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
    # Супервизора нет, поэтому страховку браузеров (5.6) зовём здесь: lost и отмена без супервизора.
    for hook in [_insurance(app), *hooks.after_exit_hooks()]:
        try:
            hook(outcome)
        except Exception as e:
            app.runs.add_error(rec.id, f"хук после выхода: {getattr(hook, '__name__', hook)!s}: {type(e).__name__}: {e}")
    return True


def _insurance(app: App, api_key: str | None = None, stop: threading.Event | None = None):
    """Страховка браузеров. Ключ из payload запуска, иначе из keyring; читается, только если есть что освобождать.
    `stop` прерывает ожидание окончания POST при отмене или выходе приложения."""

    def key() -> str | None:
        if api_key:
            return api_key
        try:
            return app.secrets.get_api_key()
        except ReplayError:
            return None

    def base_url() -> str | None:
        try:
            return app.config.base_url or None
        except ReplayError:
            return None

    return insurance.hook(key, base_url, stop=stop)


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
    """Есть живой процесс с таким pid (любой, не обязательно наш)."""
    if not pid:
        return False
    if _IS_WINDOWS:
        return procinfo.start_time(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _rebooted_since(rec: Run) -> bool:
    """Система перезагружалась после создания запуска: ни одного его процесса уже нет."""
    boot = procinfo.boot_time()
    created = parse_ts(rec.created_at)
    return boot is not None and created is not None and created.timestamp() < boot - procinfo.TOLERANCE


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _leader_alive(rec: Run) -> bool:
    """Корень дерева процессов запуска (тот же процесс, не новый с тем же pid) ещё жив."""
    if not rec.pid or rec.pid_started is None or _rebooted_since(rec):
        return False
    return procinfo.same_process(rec.pid, rec.pid_started)


def _own_group(rec: Run) -> bool:
    """Дерево процессов скрипта этого запуска ещё живо. Чужое дерево с тем же pid сюда не подходит."""
    if not rec.pid or rec.pid_started is None or _rebooted_since(rec):
        return False
    if _IS_WINDOWS:
        return procinfo.same_process(rec.pid, rec.pid_started)
    if not _group_alive(rec.pid):
        return False
    if procinfo.start_time(rec.pid) is None:
        # Лидер группы уже умер (или зомби), а группа жива. Пока существует группа
        # с таким id, ОС не выдаёт этот pid новому процессу, значит это остатки нашего дерева.
        return True
    try:
        leader_of_own_group = os.getpgid(rec.pid) == rec.pid
    except ProcessLookupError:
        return False
    return leader_of_own_group and procinfo.same_process(rec.pid, rec.pid_started)
