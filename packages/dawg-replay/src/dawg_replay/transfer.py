"""Файл результата одного запуска как значение параметра `table` или `file` другого,
выходное значение (контракт 1.5) как значение параметра любого несекретного типа, кроме table и file.

Ссылка на результат как значение параметра, в `--set`, форме, пресете и расписании:

    run:<id запуска>:<файл>
    last:<скрипт>:<файл>          последний успешный запуск скрипта
    last:<скрипт>:<файл>:partial  последний успешный или частичный
    run:<id запуска>:=<имя>       выходное значение запуска
    last:<скрипт>:=<имя>[:partial]

Файл задаётся именем, шаблоном (`prices-*.csv`) или `@main` (главный файл из `kit.result`).
Подстановка проверяет таблицу по колонкам и типам параметра; ошибка: ReplayError `input_error`,
запуск не начинается. Этим же пользуются шаги проекта: файлы стрелки из шага-источника.

    ref = parse_ref("last:wb-prices:prices.csv")
    fill = fill_ref(app, manifest.param("items"), ref)
    fill = fill_run(app, manifest.param("items"), run, "prices.csv")
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from dawg_replay_kit.manifest import Manifest, Param, check_value
from dawg_replay_kit.tables import TableError, convert_types, parse_table

from dawg_replay.errors import ReplayError
from dawg_replay.runner import last_result
from dawg_replay.runs import Run

if TYPE_CHECKING:
    from dawg_replay.app import App

CONDS = ("ok", "partial", "any")
COND_STATUSES = {"ok": ("ok",), "partial": ("ok", "partial"), "any": ("ok", "partial", "failed")}
COND_WORDS = {"ok": "последний успешный", "partial": "последний успешный или частичный", "any": "последний завершённый"}
MAIN = "@main"
TRANSFER_TYPES = ("table", "file")
# Такие запуски не шли (скрипт не стартовал или его отменил человек): не источник файлов.
NOT_REAL = ("not_started", "skipped", "cancelled", "start_error")
INPUT_ERROR = "input_error"  # код ошибки подстановки: запуск не начинается (not_started)


@dataclass(frozen=True)
class Transfer:
    file: str  # имя, шаблон или @main
    param: str

    def to_dict(self) -> dict[str, str]:
        return {"file": self.file, "param": self.param}

    @property
    def label(self) -> str:
        return f"{'главный файл' if self.file == MAIN else self.file} → {self.param}"


@dataclass(frozen=True)
class Ref:
    """Ссылка на результат: конкретный запуск (`run_id`) или последний подходящий скрипта (`script`)."""

    file: str
    run_id: str | None = None
    script: str | None = None
    partial: bool = False
    value: str | None = None  # выходное значение (контракт 1.5) вместо файла; file тогда пустой

    @property
    def cond(self) -> str:
        return "partial" if self.partial else "ok"

    @property
    def target(self) -> str:
        return f"={self.value}" if self.value is not None else self.file

    def text(self) -> str:
        if self.run_id:
            return f"run:{self.run_id}:{self.target}"
        return f"last:{self.script}:{self.target}" + (":partial" if self.partial else "")

    def fits(self, p: Param) -> bool:
        """Подходит ли ссылка параметру: файл в несекретный table/file, значение в несекретный остальной."""
        if p.is_secret:
            return False
        return (p.type not in TRANSFER_TYPES) if self.value is not None else (p.type in TRANSFER_TYPES)


def parse_ref(value: Any) -> Ref | None:
    """`run:<id>:<файл>` или `last:<скрипт>:<файл>[:partial]`; иначе None."""
    if not isinstance(value, str):
        return None
    kind, _, rest = value.partition(":")
    if kind == "run":
        run_id, _, file = rest.partition(":")
        if not run_id or not file:
            return None
        return _value_ref(Ref(file=file, run_id=run_id))
    if kind == "last":
        script, _, file = rest.partition(":")
        partial = file.endswith(":partial")
        if partial:
            file = file[: -len(":partial")]
        return _value_ref(Ref(file=file, script=script, partial=partial)) if script and file else None
    return None


def _value_ref(ref: Ref) -> Ref | None:
    """`=имя` вместо файла: ссылка на выходное значение. Имя как у параметра."""
    if not ref.file.startswith("="):
        return ref
    name = ref.file[1:]
    if not name or not (name[0].isascii() and name[0].isalpha()) or not all(
            c.isascii() and (c.isalnum() or c == "_") for c in name):
        return None
    return Ref(file="", run_id=ref.run_id, script=ref.script, partial=ref.partial, value=name)


def param_ref(p: Param, value: Any) -> Ref | None:
    """Ссылка на результат, если значение ею является и подходит параметру."""
    ref = parse_ref(value)
    return ref if ref is not None and ref.fits(p) else None


def is_ref(value: Any) -> bool:
    return parse_ref(value) is not None


def file_spec_problem(spec: str) -> str | None:
    """Имя или шаблон файла в каталоге результата: относительный путь без `..`."""
    if not spec or not spec.strip():
        return "файл не задан"
    if spec == MAIN:
        return None
    if spec.startswith(("/", "\\")) or (len(spec) > 1 and spec[1] == ":"):
        return f"`{spec}`: нужно имя файла в каталоге результата, а не полный путь"
    if ".." in PurePosixPath(spec.replace("\\", "/")).parts:
        return f"`{spec}`: выходить из каталога результата нельзя"
    if any(ch in spec for ch in "\n\r\t"):
        return f"`{spec}`: в имени файла управляющие символы"
    return None


def suitable(cond: str, status: str, reason: str | None) -> bool:
    """Подходит ли исход источника под условие (`ok`, `partial`, `any`). Незапуск и отмена не подходят никогда."""
    if reason in NOT_REAL:
        return False
    return status in COND_STATUSES.get(cond, ())


def is_pattern(spec: str) -> bool:
    return any(ch in spec for ch in "*?[")


def output_dir(app: App, run_id: str) -> Path:
    return app.paths.run_dir(run_id) / "output"


def source_files(app: App, run: Run, spec: str) -> list[Path]:
    """Файлы запуска по имени, шаблону или @main. Шаблон: все совпадения, самые новые первыми."""
    root = output_dir(app, run.id)
    if not root.is_dir():
        return []
    root = root.resolve()

    def inside(p: Path) -> bool:
        try:
            p.resolve().relative_to(root)
        except (ValueError, OSError):
            return False
        return p.is_file()

    if spec == MAIN:
        res = last_result(app.paths.run_dir(run.id) / "events.jsonl") or {}
        main = res.get("main")
        if not isinstance(main, str) or not main:
            return []
        p = root / main
        return [p] if inside(p) else []
    if file_spec_problem(spec):
        return []
    if is_pattern(spec):
        found = [p for p in root.glob(spec) if inside(p)]
        return sorted(found, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    p = root / spec
    return [p] if inside(p) else []


def declared_outputs(manifest: Manifest | None) -> tuple[Any, ...]:
    """Объявленные файлы результата (контракт 1.4), без выходных значений: у каждого name, title, columns, main."""
    return tuple(o for o in (getattr(manifest, "outputs", None) or ()) if not o.is_value) if manifest is not None else ()


def value_outputs(manifest: Manifest | None) -> tuple[Any, ...]:
    """Объявленные выходные значения (контракт 1.5, kind = "value")."""
    outs = tuple(getattr(manifest, "outputs", None) or ()) if manifest is not None else ()
    return tuple(o for o in outs if getattr(o, "kind", "file") == "value")


def output_for(manifest: Manifest | None, spec: str) -> tuple[bool, Any | None]:
    """(выходы объявлены, подходящий выход или None)."""
    outs = tuple(o for o in declared_outputs(manifest) if getattr(o, "kind", "file") != "value")
    if not outs:
        return False, None
    for o in outs:
        name = str(getattr(o, "name", "") or "")
        if spec == MAIN:
            if getattr(o, "main", False):
                return True, o
        elif name == spec or fnmatch.fnmatchcase(name, spec) or fnmatch.fnmatchcase(spec, name):
            return True, o
    return True, None


@dataclass
class Fill:
    """Значение параметра из результата другого запуска."""

    param: str
    value: Any  # file: {"path", "name", "size"} или список; table: строки
    files: list[Path]  # исходные файлы запуска-источника
    entry: dict[str, Any]  # строка снимка в runs.inputs_json
    table_files: list[Path] = field(default_factory=list)  # table: что скопировать в каталог входа
    extra_entries: list[dict[str, Any]] = field(default_factory=list)  # склейка нескольких источников


def _title(app: App, script: str) -> str:
    try:
        return app.registry.get(script).title or script
    except ReplayError:
        return script


def _fill_from(app: App, p: Param, run: Run, spec: str, title: str) -> tuple[Any, list[Path]]:
    """Файлы запуска в значение параметра. Ошибка: ReplayError input_error с понятной причиной."""
    found = source_files(app, run, spec)
    shown = "главный файл" if spec == MAIN else spec
    if not found:
        raise ReplayError(INPUT_ERROR, f"{title}: в запуске {run.id} нет файла {shown}, он нужен параметру `{p.name}`")
    if p.type == "file":
        picked = found if p.multiple else found[:1]
        recs = [{"path": str(f.resolve()), "name": f.name, "size": f.stat().st_size} for f in picked]
        value: Any = recs if p.multiple else recs[0]
        problem = check_value(p, value)
        if problem:
            raise ReplayError(INPUT_ERROR, f"{title}, {picked[0].name}: {problem}, нужен параметру `{p.name}`")
        return value, picked
    f = found[0]
    try:
        rows = convert_types(parse_table(f.read_bytes(), f.name, sheet=p.sheet), p.types)
    except TableError as e:
        msg = str(e).removeprefix(f"{f.name}: ").removeprefix(f"{f.name}, ")
        raise ReplayError(INPUT_ERROR, f"{title}, {f.name}: {msg}, нужна параметру `{p.name}`") from None
    except OSError as e:
        raise ReplayError(INPUT_ERROR, f"{title}, {f.name}: файл не читается ({e.strerror or e})") from None
    problem = check_value(p, rows)
    if problem:
        raise ReplayError(INPUT_ERROR, f"{title}, {f.name}: {problem}, нужна параметру `{p.name}`")
    return rows, [f]


def _entry(p: Param, run: Run, spec: str, files: list[Path], **extra: Any) -> dict[str, Any]:
    return {"param": p.name, "source": run.script_name, "run": run.id, "file": spec,
            "files": [f.name for f in files], **extra}


def fill_ref(app: App, p: Param, ref: Ref) -> Fill:
    """Значение из ссылки run:/last:. Нет запуска, файла или значения: ReplayError input_error."""
    if not ref.fits(p):
        if ref.value is not None:
            raise ReplayError(INPUT_ERROR, f"`{p.name}`: выходное значение подходит несекретным параметрам, кроме table и file")
        raise ReplayError(INPUT_ERROR, f"`{p.name}`: ссылка на результат подходит только несекретным table и file")
    if ref.run_id:
        try:
            run = app.runs.get(ref.run_id)
        except ReplayError:
            raise ReplayError(INPUT_ERROR, f"`{p.name}`: запуска {ref.run_id} нет") from None
        if not suitable("any", run.status, run.reason):
            raise ReplayError(INPUT_ERROR, f"`{p.name}`: запуск {ref.run_id} не завершён или не шёл ({run.label})")
    else:
        run = app.runs.last_finished(ref.script, COND_STATUSES[ref.cond])
        if run is None:
            raise ReplayError(INPUT_ERROR, f"{_title(app, ref.script)}: нет результата ({COND_WORDS[ref.cond]}), "
                                          f"он нужен параметру `{p.name}`")
    title = _title(app, run.script_name)
    if ref.value is not None:
        return fill_value(app, p, run, ref.value, ref=ref.text())
    value, files = _fill_from(app, p, run, ref.file, title)
    return Fill(p.name, value, files, _entry(p, run, ref.file, files, ref=ref.text()),
                files if p.type == "table" else [])


def fill_value(app: App, p: Param, run: Run, name: str, **extra: Any) -> Fill:
    """Выходное значение `name` запуска в параметр (шаг проекта и ссылки =имя). Число в текст
    превращается строкой; не подходит по типу: ReplayError input_error."""
    from dawg_replay import run_values

    title = _title(app, run.script_name)
    got = run_values.read(app, run.id)
    if name not in got:
        raise ReplayError(INPUT_ERROR, f"{title}: в запуске {run.id} нет значения `{name}`, оно нужно параметру `{p.name}`")
    value = run_values.coerce(p, got[name])
    problem = check_value(p, value)
    if problem:
        raise ReplayError(INPUT_ERROR, f"{title}, значение `{name}`: {problem}, нужно параметру `{p.name}`")
    entry = {"param": p.name, "source": run.script_name, "run": run.id, "value": name, "files": [], **extra}
    return Fill(p.name, value, [], entry, [])




def fill_run(app: App, p: Param, run: Run, spec: str, **extra: Any) -> Fill:
    """Значение из файла `spec` конкретного запуска (шаг проекта). Ошибка: ReplayError input_error."""
    if p.type not in TRANSFER_TYPES or p.secret:
        raise ReplayError(INPUT_ERROR, f"`{p.name}`: файл результата подходит только несекретным table и file")
    title = _title(app, run.script_name)
    value, files = _fill_from(app, p, run, spec, title)
    return Fill(p.name, value, files, _entry(p, run, spec, files, **extra), files if p.type == "table" else [])
