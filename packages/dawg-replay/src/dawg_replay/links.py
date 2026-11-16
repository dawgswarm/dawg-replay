"""Связи между скриптами: «запускать после» и «передать файл результата в параметр» (M6).

Связь соединяет источник и получателя, на пару скриптов одна связь:

- `cond`: какой исход источника подходит: `ok` (успешный), `partial` (успешный или частичный),
  `any` (любой запуск, который действительно шёл);
- `starts`: запускать ли получателя после подходящего исхода; `False` значит «только файл»;
- `files`: передачи «файл результата источника → параметр получателя» (`table` или `file`).
  Файл задаётся именем, шаблоном (`prices-*.csv`) или `@main` (главный файл из `kit.result`);
- `preset`: пресет получателя для запуска по связи; `enabled`; `limit_per_day`: предел запусков
  по связи за календарные сутки, без него связь не может замыкать круг.

Режим получателя с несколькими источниками (`all` или `any`, срок ожидания, что делать по сроку)
здесь только хранится. Сам автозапуск и журнал его решений ведёт dawg_replay.chain (dr-106).

Ссылка на результат как значение параметра `table` или `file`, в `--set`, форме и пресете:

    run:<id запуска>:<файл>
    last:<скрипт>:<файл>          последний успешный запуск скрипта
    last:<скрипт>:<файл>:partial  последний успешный или частичный

    links = app.links
    link, warnings = links.add("wb-prices", "update-prices", files=[Transfer("prices.csv", "items")])
    problems = links.check()               # вся схема без запуска
    run = links.last_suitable_run("wb-prices", "ok")
"""

from __future__ import annotations

import fnmatch
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from dawg_replay_kit.manifest import Manifest, Param, check_value
from dawg_replay_kit.tables import TableError, convert_types, parse_table

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.presets import normalize as normalize_preset
from dawg_replay.runner import last_result
from dawg_replay.runs import Run

if TYPE_CHECKING:
    from dawg_replay.app import App

CONDS = ("ok", "partial", "any")
COND_STATUSES = {"ok": ("ok",), "partial": ("ok", "partial"), "any": ("ok", "partial", "failed")}
COND_WORDS = {"ok": "последний успешный", "partial": "последний успешный или частичный", "any": "последний завершённый"}
AFTER_CHOICES = ("ok", "partial", "any", "never")  # CLI --after: never это «только файл»
MODES = ("all", "any")
ON_TIMEOUT = ("skip", "run")
DEFAULT_WAIT_SECONDS = 6 * 3600
MAIN = "@main"
TRANSFER_TYPES = ("table", "file")
NOT_REAL = ("not_started", "skipped", "cancelled")  # такие запуски не шли: не повод и не источник файлов
LINK_INPUT = "link_input"  # код ошибки подстановки: запуск не начинается (not_started)


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
class Link:
    id: int
    source: str
    target: str
    cond: str = "ok"
    starts: bool = True
    preset: str | None = None
    enabled: bool = True
    limit_per_day: int | None = None
    files: tuple[Transfer, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    @property
    def after(self) -> str:
        """Как в CLI: ok, partial, any или never (только файл)."""
        return self.cond if self.starts else "never"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "cond": self.cond,
            "starts": self.starts,
            "after": self.after,
            "preset": self.preset,
            "enabled": self.enabled,
            "limit_per_day": self.limit_per_day,
            "files": [t.to_dict() for t in self.files],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class TargetMode:
    script: str
    mode: str = "all"
    wait_seconds: int = DEFAULT_WAIT_SECONDS
    on_timeout: str = "skip"

    def to_dict(self) -> dict[str, Any]:
        return {"script": self.script, "mode": self.mode, "wait_seconds": self.wait_seconds, "on_timeout": self.on_timeout}


@dataclass(frozen=True)
class Problem:
    level: str  # error: связь не сохраняется (или не сработает), warning: сохранить можно
    text: str
    link_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "text": self.text, "link_id": self.link_id}


@dataclass(frozen=True)
class Ref:
    """Ссылка на результат: конкретный запуск (`run_id`) или последний подходящий скрипта (`script`)."""

    file: str
    run_id: str | None = None
    script: str | None = None
    partial: bool = False

    @property
    def cond(self) -> str:
        return "partial" if self.partial else "ok"

    def text(self) -> str:
        if self.run_id:
            return f"run:{self.run_id}:{self.file}"
        return f"last:{self.script}:{self.file}" + (":partial" if self.partial else "")


def parse_ref(value: Any) -> Ref | None:
    """`run:<id>:<файл>` или `last:<скрипт>:<файл>[:partial]`; иначе None."""
    if not isinstance(value, str):
        return None
    kind, _, rest = value.partition(":")
    if kind == "run":
        run_id, _, file = rest.partition(":")
        return Ref(file=file, run_id=run_id) if run_id and file else None
    if kind == "last":
        script, _, file = rest.partition(":")
        partial = file.endswith(":partial")
        if partial:
            file = file[: -len(":partial")]
        return Ref(file=file, script=script, partial=partial) if script and file else None
    return None


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
    """Подходит ли исход источника под условие связи. Незапуск и отмена не подходят никогда."""
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


def _outputs(manifest: Manifest | None) -> tuple[Any, ...]:
    """Объявленные выходы (контракт 1.4, dr-105): у каждого name, title, columns, main."""
    return tuple(getattr(manifest, "outputs", None) or ()) if manifest is not None else ()


def _output_for(manifest: Manifest | None, spec: str) -> tuple[bool, Any | None]:
    """(выходы объявлены, подходящий выход или None)."""
    outs = _outputs(manifest)
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


def _row_link(row: sqlite3.Row, files: tuple[Transfer, ...]) -> Link:
    return Link(
        id=row["id"], source=row["source"], target=row["target"], cond=row["cond"], starts=bool(row["starts"]),
        preset=row["preset"], enabled=bool(row["enabled"]), limit_per_day=row["limit_per_day"], files=files,
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class Links:
    def __init__(self, conn: sqlite3.Connection, app: App):
        self._db = conn
        self._app = app

    # --- чтение ---

    def _files(self, link_id: int) -> tuple[Transfer, ...]:
        rows = self._db.execute("SELECT file, param FROM link_files WHERE link_id = ? ORDER BY pos", (link_id,))
        return tuple(Transfer(r["file"], r["param"]) for r in rows)

    def _load(self, rows: Any) -> list[Link]:
        return [_row_link(r, self._files(r["id"])) for r in rows]

    def list(self, script: str | None = None) -> list[Link]:
        """Все связи или связи, где скрипт источник или получатель."""
        if script is None:
            rows = self._db.execute("SELECT * FROM links ORDER BY source, target")
        else:
            rows = self._db.execute("SELECT * FROM links WHERE source = ? OR target = ? ORDER BY source, target",
                                    (script, script))
        return self._load(rows)

    def incoming(self, target: str) -> list[Link]:
        return self._load(self._db.execute("SELECT * FROM links WHERE target = ? ORDER BY source", (target,)))

    def outgoing(self, source: str) -> list[Link]:
        return self._load(self._db.execute("SELECT * FROM links WHERE source = ? ORDER BY target", (source,)))

    def get(self, link_id: int) -> Link:
        row = self._db.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise ReplayError("link_not_found", f"связи {link_id} нет; список: dawg-replay link list")
        return _row_link(row, self._files(row["id"]))

    def find(self, source: str, target: str) -> Link | None:
        row = self._db.execute("SELECT * FROM links WHERE source = ? AND target = ?", (source, target)).fetchone()
        return _row_link(row, self._files(row["id"])) if row else None

    def resolve(self, source_or_id: str, target: str | None = None) -> Link:
        """Связь по id или по паре «источник получатель» (так её называет CLI)."""
        if target is None:
            if source_or_id.isdigit():
                return self.get(int(source_or_id))
            raise ReplayError("link_not_found", "укажите связь: id или источник и получатель")
        link = self.find(source_or_id, target)
        if link is None:
            raise ReplayError("link_not_found", f"связи {source_or_id} → {target} нет; список: dawg-replay link list")
        return link

    # --- изменение ---

    def add(
        self,
        source: str,
        target: str,
        *,
        cond: str = "ok",
        starts: bool = True,
        files: tuple[Transfer, ...] | list[Transfer] = (),
        preset: str | None = None,
        limit_per_day: int | None = None,
        enabled: bool = True,
    ) -> tuple[Link, list[Problem]]:
        """Новая связь. Ошибки проверки: ReplayError `link_invalid` (details: список Problem.to_dict),
        предупреждения возвращаются вторым значением."""
        if self.find(source, target) is not None:
            raise ReplayError("link_exists", f"связь {source} → {target} уже есть; изменить: dawg-replay link set {source} {target} ...")
        now = utcnow()
        link = Link(0, source, target, cond, bool(starts), normalize_preset(preset) or None, bool(enabled),
                    limit_per_day, tuple(files), now, now)
        warnings = self._validate(link)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute(
                "INSERT INTO links (source, target, cond, starts, preset, enabled, limit_per_day, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, target, link.cond, int(link.starts), link.preset, int(link.enabled), link.limit_per_day, now, now),
            )
            self._write_files(cur.lastrowid, link.files)
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            self._db.execute("ROLLBACK")
            raise ReplayError("link_exists", f"связь {source} → {target} уже есть") from None
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return self.get(cur.lastrowid), warnings

    _UNSET: Any = object()

    def update(
        self,
        link_id: int,
        *,
        cond: str | None = None,
        starts: bool | None = None,
        files: tuple[Transfer, ...] | list[Transfer] | None = None,
        preset: Any = _UNSET,
        limit_per_day: Any = _UNSET,
        enabled: bool | None = None,
    ) -> tuple[Link, list[Problem]]:
        """Меняет переданные поля. `preset=None`, `limit_per_day=None` снимают значение."""
        old = self.get(link_id)
        new = replace(
            old,
            cond=old.cond if cond is None else cond,
            starts=old.starts if starts is None else bool(starts),
            files=old.files if files is None else tuple(files),
            preset=old.preset if preset is self._UNSET else (normalize_preset(preset) or None),
            limit_per_day=old.limit_per_day if limit_per_day is self._UNSET else limit_per_day,
            enabled=old.enabled if enabled is None else bool(enabled),
            updated_at=utcnow(),
        )
        warnings = self._validate(new)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute(
                "UPDATE links SET cond = ?, starts = ?, preset = ?, enabled = ?, limit_per_day = ?, updated_at = ? WHERE id = ?",
                (new.cond, int(new.starts), new.preset, int(new.enabled), new.limit_per_day, new.updated_at, link_id),
            )
            if files is not None:
                self._db.execute("DELETE FROM link_files WHERE link_id = ?", (link_id,))
                self._write_files(link_id, new.files)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return self.get(link_id), warnings

    def set_enabled(self, link_id: int, enabled: bool) -> Link:
        self.get(link_id)
        self._db.execute("UPDATE links SET enabled = ?, updated_at = ? WHERE id = ?", (int(enabled), utcnow(), link_id))
        return self.get(link_id)

    def remove(self, link_id: int) -> Link:
        link = self.get(link_id)
        self._db.execute("DELETE FROM links WHERE id = ?", (link_id,))
        self._db.execute("DELETE FROM link_log WHERE link_id = ?", (link_id,))
        return link

    def remove_script(self, name: str) -> list[int]:
        """Удалённый скрипт уносит свои связи и режим получателя."""
        ids = [r["id"] for r in self._db.execute("SELECT id FROM links WHERE source = ? OR target = ?", (name, name))]
        for i in ids:
            self._db.execute("DELETE FROM links WHERE id = ?", (i,))
            self._db.execute("DELETE FROM link_log WHERE link_id = ?", (i,))
        self._db.execute("DELETE FROM link_targets WHERE script = ?", (name,))
        return ids

    def _write_files(self, link_id: int, files: tuple[Transfer, ...]) -> None:
        for pos, t in enumerate(files):
            self._db.execute("INSERT INTO link_files (link_id, pos, file, param) VALUES (?, ?, ?, ?)",
                             (link_id, pos, t.file, t.param))

    # --- режим получателя ---

    def mode(self, target: str) -> TargetMode:
        row = self._db.execute("SELECT * FROM link_targets WHERE script = ?", (target,)).fetchone()
        if row is None:
            return TargetMode(target)
        return TargetMode(target, row["mode"], row["wait_seconds"], row["on_timeout"])

    def set_mode(self, target: str, mode: str | None = None, wait_seconds: int | None = None,
                 on_timeout: str | None = None) -> TargetMode:
        self._app.registry.get(target)
        old = self.mode(target)
        new = TargetMode(target, old.mode if mode is None else mode,
                         old.wait_seconds if wait_seconds is None else wait_seconds,
                         old.on_timeout if on_timeout is None else on_timeout)
        if new.mode not in MODES:
            raise ReplayError("bad_link", f"режим all (после всех) или any (после любого), получено {new.mode!r}")
        if new.on_timeout not in ON_TIMEOUT:
            raise ReplayError("bad_link", f"по сроку skip (пропустить) или run (запустить с тем, что есть), получено {new.on_timeout!r}")
        if not isinstance(new.wait_seconds, int) or isinstance(new.wait_seconds, bool) or not 60 <= new.wait_seconds <= 7 * 86400:
            raise ReplayError("bad_link", "срок ожидания от 1 минуты до 7 суток")
        self._db.execute(
            "INSERT INTO link_targets (script, mode, wait_seconds, on_timeout) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (script) DO UPDATE SET mode = excluded.mode, wait_seconds = excluded.wait_seconds,"
            " on_timeout = excluded.on_timeout",
            (target, new.mode, new.wait_seconds, new.on_timeout),
        )
        return new

    # --- журнал решений ---

    def log(self, link_id: int, decision: str, text: str, source_run: str | None = None,
            target_run: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO link_log (link_id, at, decision, text, source_run, target_run) VALUES (?, ?, ?, ?, ?, ?)",
            (link_id, utcnow(), decision, text, source_run, target_run),
        )

    def journal(self, link_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT at, decision, text, source_run, target_run FROM link_log WHERE link_id = ? ORDER BY id DESC LIMIT ?",
            (link_id, limit),
        )
        return [dict(r) for r in rows]

    # --- источники ---

    def last_suitable_run(self, source: str, cond: str) -> Run | None:
        return self._app.runs.last_finished(source, COND_STATUSES[cond])

    # --- проверка ---

    def _manifest(self, name: str) -> tuple[Manifest | None, str | None]:
        """(манифест, текст ошибки): скрипта нет или манифест текущей версии не читается."""
        try:
            self._app.registry.get(name)
        except ReplayError:
            return None, f"скрипта `{name}` нет"
        try:
            return self._app.registry.manifest(name), None
        except ReplayError as e:
            return None, f"скрипт `{name}` сломан, запуск по связи не начнётся: {e.message}"

    def _validate(self, link: Link) -> list[Problem]:
        """Проверка перед сохранением: ошибки поднимаются, предупреждения возвращаются."""
        if link.cond not in CONDS:
            raise ReplayError("bad_link", f"условие ok, partial или any, получено {link.cond!r}")
        if link.limit_per_day is not None and (
            not isinstance(link.limit_per_day, int) or isinstance(link.limit_per_day, bool) or link.limit_per_day < 1
        ):
            raise ReplayError("bad_link", "предел запусков в сутки: целое от 1")
        others = [x for x in self.list() if x.id != link.id]
        problems = self._problems(link, others)
        errors = [p for p in problems if p.level == "error"]
        if errors:
            text = "связь не сохранена:\n" + "\n".join(f"  {p.text}" for p in errors)
            raise ReplayError("link_invalid", text, [p.to_dict() for p in problems])
        return problems

    def check(self, script: str | None = None) -> list[Problem]:
        """Вся схема (или связи скрипта) без запуска: параметры, колонки, круги, сломанные скрипты."""
        links = self.list()
        chosen = links if script is None else [x for x in links if script in (x.source, x.target)]
        if script is not None:
            self._app.registry.get(script)
        out: list[Problem] = []
        seen_cycles: set[frozenset[int]] = set()
        for link in chosen:
            others = [x for x in links if x.id != link.id]
            for p in self._problems(link, others, cycles=False):
                out.append(p)
            cyc = self._cycle(link, others)
            if cyc is not None:
                ids, names = cyc
                if frozenset(ids) not in seen_cycles:
                    seen_cycles.add(frozenset(ids))
                    out.append(Problem("error", self._cycle_text(names), link.id))
        return out

    def _problems(self, link: Link, others: list[Link], cycles: bool = True) -> list[Problem]:
        out: list[Problem] = []

        def err(text: str) -> None:
            out.append(Problem("error", text, link.id or None))

        def warn(text: str) -> None:
            out.append(Problem("warning", text, link.id or None))

        name = f"{link.source} → {link.target}"
        src_m, src_err = self._manifest(link.source)
        dst_m, dst_err = self._manifest(link.target)
        if src_err:
            err(f"{name}: {src_err}")
        if dst_err:
            err(f"{name}: {dst_err}")
        if not link.starts and not link.files:
            err(f"{name}: связь без запуска и без передач файлов ничего не делает")
        if link.preset and dst_m is not None:
            try:
                self._app.presets.get(link.target, link.preset, dst_m)
            except ReplayError:
                err(f"{name}: у скрипта `{link.target}` нет пресета «{link.preset}»")
        taken = {t.param: x for x in others if x.target == link.target for t in x.files}
        seen: set[str] = set()
        for t in link.files:
            bad = file_spec_problem(t.file)
            if bad:
                err(f"{name}: {bad}")
            if t.param in seen:
                err(f"{name}: в параметр `{t.param}` уже передаётся файл этой связи")
            seen.add(t.param)
            if t.param in taken:
                err(f"{name}: в параметр `{t.param}` уже передаёт файл связь {taken[t.param].source} → {link.target}")
            if dst_m is None:
                continue
            try:
                p = dst_m.param(t.param)
            except KeyError:
                err(f"{name}: у скрипта `{link.target}` нет параметра `{t.param}`")
                continue
            if p.type not in TRANSFER_TYPES:
                err(f"{name}: параметр `{t.param}` типа {p.type}, файл передаётся только в table или file")
                continue
            if p.secret:
                err(f"{name}: параметр `{t.param}` секретный, передавать в него файл по связи нельзя")
                continue
            declared, out_decl = _output_for(src_m, t.file)
            if declared and out_decl is None:
                what = "главный файл" if t.file == MAIN else f"файл `{t.file}`"
                warn(f"{name}: {what} не объявлен в выходах скрипта `{link.source}`")
            elif out_decl is not None and p.type == "table" and p.columns:
                have = getattr(out_decl, "columns", None)
                if have:
                    lack = [c for c in p.columns if c not in tuple(have)]
                    if lack:
                        cols = ", ".join(f"`{c}`" for c in lack)
                        word = "колонки" if len(lack) == 1 else "колонок"
                        warn(f"{name}: в `{getattr(out_decl, 'name', t.file)}` нет {word} {cols}, "
                             f"нужны параметру `{t.param}`")
        if cycles:
            cyc = self._cycle(link, others)
            if cyc is not None:
                err(self._cycle_text(cyc[1]))
        return out

    @staticmethod
    def _cycle_text(names: list[str]) -> str:
        return ("круг без предела: " + " → ".join(names) +
                ". Задайте на одной из связей круга предел запусков в сутки (--limit-per-day N)")

    @staticmethod
    def _cycle(link: Link, others: list[Link]) -> tuple[list[int], list[str]] | None:
        """Круг из связей с запуском без предела, который замыкает эта связь: (id связей, имена по кругу)."""
        if not link.starts or link.limit_per_day is not None:
            return None
        edges: dict[str, list[Link]] = {}
        for x in others:
            if x.starts and x.limit_per_day is None:
                edges.setdefault(x.source, []).append(x)
        # Путь от получателя назад к источнику по связям без предела.
        stack: list[tuple[str, list[Link]]] = [(link.target, [])]
        visited: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node == link.source:
                chain = [link, *path]
                return [x.id for x in chain], [link.source, *[x.target for x in chain]]
            if node in visited:
                continue
            visited.add(node)
            for x in edges.get(node, ()):
                stack.append((x.target, [*path, x]))
        return None


# --- подстановка файлов в запуск (dawg_replay.launch.prepare) ---


@dataclass
class Fill:
    """Значение параметра из результата другого запуска."""

    param: str
    value: Any  # file: {"path", "name", "size"} или список; table: строки
    files: list[Path]  # исходные файлы запуска-источника
    entry: dict[str, Any]  # строка снимка в runs.inputs_json
    table_files: list[Path] = field(default_factory=list)  # table: что скопировать в каталог входа


def _title(app: App, script: str) -> str:
    try:
        return app.registry.get(script).title or script
    except ReplayError:
        return script


def _fill_from(app: App, p: Param, run: Run, spec: str, title: str) -> tuple[Any, list[Path]]:
    """Файлы запуска в значение параметра. Ошибка: ReplayError link_input с понятной причиной."""
    found = source_files(app, run, spec)
    shown = "главный файл" if spec == MAIN else spec
    if not found:
        raise ReplayError(LINK_INPUT, f"{title}: в запуске {run.id} нет файла {shown}, он нужен параметру `{p.name}`")
    if p.type == "file":
        picked = found if p.multiple else found[:1]
        recs = [{"path": str(f.resolve()), "name": f.name, "size": f.stat().st_size} for f in picked]
        value: Any = recs if p.multiple else recs[0]
        problem = check_value(p, value)
        if problem:
            raise ReplayError(LINK_INPUT, f"{title}, {picked[0].name}: {problem}, нужен параметру `{p.name}`")
        return value, picked
    f = found[0]
    try:
        rows = convert_types(parse_table(f.read_bytes(), f.name, sheet=p.sheet), p.types)
    except TableError as e:
        msg = str(e).removeprefix(f"{f.name}: ").removeprefix(f"{f.name}, ")
        raise ReplayError(LINK_INPUT, f"{title}, {f.name}: {msg}, нужна параметру `{p.name}`") from None
    except OSError as e:
        raise ReplayError(LINK_INPUT, f"{title}, {f.name}: файл не читается ({e.strerror or e})") from None
    problem = check_value(p, rows)
    if problem:
        raise ReplayError(LINK_INPUT, f"{title}, {f.name}: {problem}, нужна параметру `{p.name}`")
    return rows, [f]


def _entry(p: Param, run: Run, spec: str, files: list[Path], **extra: Any) -> dict[str, Any]:
    return {"param": p.name, "source": run.script_name, "run": run.id, "file": spec,
            "files": [f.name for f in files], **extra}


def fill_ref(app: App, p: Param, ref: Ref) -> Fill:
    """Значение из ссылки run:/last:. Нет запуска или файла: ReplayError link_input."""
    if p.type not in TRANSFER_TYPES or p.secret:
        raise ReplayError(LINK_INPUT, f"`{p.name}`: ссылка на результат подходит только несекретным table и file")
    if ref.run_id:
        try:
            run = app.runs.get(ref.run_id)
        except ReplayError:
            raise ReplayError(LINK_INPUT, f"`{p.name}`: запуска {ref.run_id} нет") from None
        if not suitable("any", run.status, run.reason):
            raise ReplayError(LINK_INPUT, f"`{p.name}`: запуск {ref.run_id} не завершён или не шёл ({run.label})")
    else:
        run = app.runs.last_finished(ref.script, COND_STATUSES[ref.cond])
        if run is None:
            raise ReplayError(LINK_INPUT, f"{_title(app, ref.script)}: нет результата ({COND_WORDS[ref.cond]}), "
                                          f"он нужен параметру `{p.name}`")
    title = _title(app, run.script_name)
    value, files = _fill_from(app, p, run, ref.file, title)
    return Fill(p.name, value, files, _entry(p, run, ref.file, files, ref=ref.text()),
                files if p.type == "table" else [])


def fill_links(app: App, manifest: Manifest, skip: set[str],
               link_sources: dict[int, str] | None = None) -> tuple[list[Fill], dict[str, str]]:
    """Передачи по включённым связям в этот скрипт для параметров не из `skip` (явно заданных).

    `link_sources`: {id связи: id запуска} какой запуск источника брать (тот, что вызвал запуск,
    или выбранный кругом «после всех»); для остальных связей берётся последний подходящий.
    Возвращает (значения, незаполненные: параметр -> причина). Незаполненный обязательный
    решает вызывающий; ошибка файла или проверки таблицы поднимает ReplayError link_input."""
    fills: list[Fill] = []
    empty: dict[str, str] = {}
    sources = link_sources or {}
    for link in app.links.incoming(manifest.name):
        if not link.enabled or not link.files:
            continue
        title = _title(app, link.source)
        run: Run | None
        if link.id in sources:
            try:
                run = app.runs.get(sources[link.id])
            except ReplayError:
                run = None
        else:
            run = app.links.last_suitable_run(link.source, link.cond)
        for t in link.files:
            if t.param in skip:
                continue
            try:
                p = manifest.param(t.param)
            except KeyError:
                raise ReplayError(LINK_INPUT, f"связь {link.source} → {link.target}: у скрипта нет параметра "
                                              f"`{t.param}`; поправьте связь: dawg-replay link check") from None
            if p.type not in TRANSFER_TYPES or p.secret:
                raise ReplayError(LINK_INPUT, f"связь {link.source} → {link.target}: в параметр `{t.param}` файл "
                                              "передавать нельзя; поправьте связь: dawg-replay link check")
            if run is None:
                empty[t.param] = (f"{title}: нет результата ({COND_WORDS[link.cond]}), "
                                  f"он нужен параметру `{t.param}`")
                continue
            value, files = _fill_from(app, p, run, t.file, title)
            fills.append(Fill(p.name, value, files, _entry(p, run, t.file, files, link_id=link.id),
                              files if p.type == "table" else []))
    return fills, empty
