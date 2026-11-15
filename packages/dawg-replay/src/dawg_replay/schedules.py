"""Регулярные запуски через системный планировщик.

Своего демона у приложения нет (ТЗ 3.2): расписание ставится в cron на macOS и
Linux и в Планировщик задач на Windows. Задача вызывает

    dawg-replay run <скрипт> --preset <пресет> --follow --schedule <id>

с полным путём к dawg-replay, каталогом uv в PATH и выводом в
`<каталог данных>/schedules/<id>.log`. Расписания хранятся в таблице `schedules`;
`list` сверяет её с планировщиком: задача пропала (missing) или осталась
сиротой (orphan).

    sch = Schedules(app)
    s = sch.add("ozon-search-cards", "daily", Spec.daily("09:00"))
    sch.list()        # {"schedules": [...], "orphans": [...]}
    sch.remove(s.id)

Чужие строки crontab не трогаются: свои помечены `# dawg-replay:<id>` в конце
строки. На Windows задачи лежат в папке `dawg-replay` Планировщика, а команда
записана в `<id>.cmd` рядом с логом (у schtasks /TR предел 261 символ).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError

ENV_SCHEDULER = "DAWG_REPLAY_SCHEDULER"  # cron | schtasks: какой планировщик (по умолчанию по ОС)
ENV_CRONTAB = "DAWG_REPLAY_CRONTAB"  # команда crontab: строка или JSON-список (для тестов)
ENV_SCHTASKS = "DAWG_REPLAY_SCHTASKS"  # команда schtasks: строка или JSON-список (для тестов)

MARKER = "dawg-replay:"
TASK_FOLDER = "dawg-replay"
EVERY_HOURS = (1, 2, 3, 4, 6, 8, 12)  # делители суток: в cron и schtasks одинаковые моменты
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_ALIASES = {
    **{d: i for i, d in enumerate(WEEKDAYS)},
    **{d: i for i, d in enumerate(("пн", "вт", "ср", "чт", "пт", "сб", "вс"))},
    **{str(i + 1): i for i in range(7)},  # 1 = понедельник
}
WEEKDAY_TITLES = ("понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям")
WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_MARK_RE = re.compile(r"#\s*" + re.escape(MARKER) + r"([A-Za-z0-9_-]+)\s*$")


# --- когда запускать --------------------------------------------------------------


def parse_time(value: str) -> str:
    m = _TIME_RE.match((value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ReplayError("bad_schedule", f"время {value!r}: нужно ЧЧ:ММ, например 09:30")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_weekday(value: str) -> int:
    key = (value or "").strip().lower()[:3]
    if key not in _WEEKDAY_ALIASES:
        raise ReplayError("bad_schedule", f"день недели {value!r}: mon..sun, пн..вс или 1..7 (1 = понедельник)")
    return _WEEKDAY_ALIASES[key]


@dataclass(frozen=True)
class Spec:
    """Когда запускать. kind: daily, weekly, hours, cron. Время местное, как у планировщика."""

    kind: str
    time: str | None = None  # ЧЧ:ММ для daily и weekly
    weekday: int | None = None  # 0 = понедельник
    hours: int | None = None
    cron: str | None = None

    @staticmethod
    def daily(time: str) -> Spec:
        return Spec("daily", time=parse_time(time))

    @staticmethod
    def weekly(weekday: str | int, time: str) -> Spec:
        day = weekday if isinstance(weekday, int) else parse_weekday(weekday)
        if not 0 <= day <= 6:
            raise ReplayError("bad_schedule", "день недели: 0..6, 0 = понедельник")
        return Spec("weekly", time=parse_time(time), weekday=day)

    @staticmethod
    def every_hours(n: int | str) -> Spec:
        try:
            hours = int(n)
        except (TypeError, ValueError):
            hours = 0
        if hours not in EVERY_HOURS:
            allowed = ", ".join(map(str, EVERY_HOURS))
            raise ReplayError("bad_schedule", f"каждые {n!r} ч: можно {allowed} (запуски в одни и те же часы каждые сутки)")
        return Spec("hours", hours=hours)

    @staticmethod
    def from_cron(expr: str) -> Spec:
        expr = " ".join((expr or "").split())
        CronExpr.parse(expr)  # проверка
        if next_run(expr, datetime(2000, 1, 1)) is None:
            raise ReplayError("bad_schedule", f"cron {expr!r} не срабатывает ни разу")
        return Spec("cron", cron=expr)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Spec:
        return Spec(d["kind"], d.get("time"), d.get("weekday"), d.get("hours"), d.get("cron"))

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in (("kind", self.kind), ("time", self.time), ("weekday", self.weekday),
                                  ("hours", self.hours), ("cron", self.cron)) if v is not None}

    def _hm(self) -> tuple[int, int]:
        h, m = (self.time or "00:00").split(":")
        return int(h), int(m)

    def cron_expr(self) -> str:
        if self.kind == "cron":
            return self.cron or ""
        if self.kind == "hours":
            return "0 * * * *" if self.hours == 1 else f"0 */{self.hours} * * *"
        h, m = self._hm()
        if self.kind == "weekly":
            return f"{m} {h} * * {(self.weekday + 1) % 7}"  # в cron 0 = воскресенье
        return f"{m} {h} * * *"

    def describe(self) -> str:
        if self.kind == "daily":
            return f"каждый день в {self.time}"
        if self.kind == "weekly":
            return f"по {WEEKDAY_TITLES[self.weekday]} в {self.time}"
        if self.kind == "hours":
            return "каждый час" if self.hours == 1 else f"каждые {self.hours} ч, с полуночи"
        return f"cron: {self.cron}"


class CronExpr:
    """Пять полей cron: минута, час, день месяца, месяц, день недели. Списки, диапазоны, шаги."""

    _RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
    _NAMES = (
        {}, {}, {},
        {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))},
        {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))},
    )

    def __init__(self, fields: list[set[int]], restricted: tuple[bool, bool]):
        self.minutes, self.hours, self.days, self.months, dow = fields
        self.dows = {0 if d == 7 else d for d in dow}
        self.dom_restricted, self.dow_restricted = restricted

    @classmethod
    def parse(cls, expr: str) -> CronExpr:
        parts = expr.split()
        if len(parts) != 5:
            raise ReplayError("bad_schedule", f"cron {expr!r}: нужно пять полей «мин час день месяц день_недели», например «30 9 * * 1-5»")
        fields = [cls._field(p, i, expr) for i, p in enumerate(parts)]
        return cls(fields, (not parts[2].startswith("*"), not parts[4].startswith("*")))

    @classmethod
    def _field(cls, text: str, i: int, expr: str) -> set[int]:
        lo, hi = cls._RANGES[i]
        out: set[int] = set()

        def num(s: str) -> int:
            s = s.lower()
            if s in cls._NAMES[i]:
                return cls._NAMES[i][s]
            if not s.isdigit():
                raise ValueError
            return int(s)

        try:
            for part in text.split(","):
                rng, _, step_s = part.partition("/")
                step = int(step_s) if step_s else 1
                if step < 1:
                    raise ValueError
                if rng == "*":
                    a, b = lo, hi
                elif "-" in rng:
                    a_s, b_s = rng.split("-", 1)
                    a, b = num(a_s), num(b_s)
                else:
                    a = num(rng)
                    b = hi if step_s else a
                if not (lo <= a <= hi and lo <= b <= hi and a <= b):
                    raise ValueError
                out.update(range(a, b + 1, step))
        except ValueError:
            raise ReplayError("bad_schedule", f"cron {expr!r}: поле {text!r} вне {lo}..{hi} или записано неверно") from None
        return out

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.weekday() + 1) % 7 in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom or dow  # так считает cron: подходит любой из двух
        return dom and dow


def next_run(expr: str, after: datetime) -> datetime | None:
    """Ближайшее срабатывание строго после `after` (наивное местное время), не дальше 5 лет."""
    c = CronExpr.parse(expr)
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    hours, minutes = sorted(c.hours), sorted(c.minutes)
    for offset in range(366 * 5):
        d = start.date() + timedelta(days=offset)
        if not c.day_matches(d):
            continue
        for h in hours:
            for m in minutes:
                t = datetime(d.year, d.month, d.day, h, m)
                if t >= start:
                    return t
    return None


# --- расписание -------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    id: str
    script: str
    preset: str
    spec: Spec
    created_at: str

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        nxt = next_run(self.spec.cron_expr(), now or datetime.now())
        return {
            "id": self.id,
            "script": self.script,
            "preset": self.preset,
            "spec": self.spec.to_dict(),
            "when": self.spec.describe(),
            "cron": self.spec.cron_expr(),
            "next_run": nxt.isoformat(timespec="minutes") if nxt else None,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class TaskPlan:
    """Что поставить в планировщик: команда, окружение и лог."""

    id: str
    spec: Spec
    argv: list[str]
    path_dirs: list[str]  # каталоги, которые добавляются в начало PATH
    env: dict[str, str] = field(default_factory=dict)
    log: Path = Path()
    script_file: Path = Path()  # обёртка .cmd для Windows


class Backend(Protocol):
    kind: str

    def installed(self) -> dict[str, str]:
        """id своих задач -> как задача записана в планировщике."""
        ...

    def install(self, plan: TaskPlan) -> None: ...

    def remove(self, sid: str) -> None: ...


def _command(env_name: str, default: str) -> list[str]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return [default]
    if raw.startswith("["):
        return [str(x) for x in json.loads(raw)]
    return [raw]


def _call(argv: Sequence[str], what: str, stdin: str | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(list(argv), input=stdin.encode("utf-8") if stdin is not None else None,
                              capture_output=True, timeout=60)
    except FileNotFoundError:
        raise ReplayError("scheduler_unavailable", f"{what} не найден: регулярные запуски на этой системе недоступны") from None
    except subprocess.TimeoutExpired:
        raise ReplayError("scheduler_error", f"{what} не ответил за 60 секунд") from None


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


_CMD_META = set('^&|<>()"')


def _cmd_arg(arg: str) -> str:
    """Аргумент для строки .cmd: имя пресета может содержать кавычки, скобки, `&`, `%`.
    Аргумент с пробелом или кавычкой берётся в кавычки по правилам разбора argv
    в Windows (CommandLineToArgvW), затем
    каждый спецсимвол cmd экранируется `^`, поэтому cmd ни разу не входит в режим
    кавычек и передаёт программе строку как есть. `%` в .cmd удваивается."""
    if arg and not any(c in arg for c in ' \t"'):
        return "".join("^" + c if c in _CMD_META else "%%" if c == "%" else c for c in arg)
    out, slashes = ['"'], 0
    for ch in arg:
        if ch == "\\":
            slashes += 1
            continue
        if ch == '"':
            out.append("\\" * (slashes * 2 + 1) + ch)
        else:
            out.append("\\" * slashes + ch)
        slashes = 0
    out.append("\\" * (slashes * 2) + '"')
    return "".join("^" + c if c in _CMD_META else "%%" if c == "%" else c for c in "".join(out))


def secret_table_gap(manifest: Any) -> str | None:
    """Обязательная секретная таблица: регулярный запуск такого скрипта невозможен ни с каким пресетом."""
    tables = [p for p in manifest.params if p.type == "table" and p.secret and p.required]
    if not tables:
        return None
    names = ", ".join(f"«{p.title}»" for p in tables)
    return (f"у скрипта обязательная секретная таблица {names}: она не сохраняется в пресетах, поэтому "
            "запуск по расписанию её не получит. Запускайте скрипт вручную и загружайте таблицу в форме")


def preset_gap(manifest: Any, preset: Any) -> str | None:
    """Почему плановый запуск по этому пресету не начнётся, или None.

    Секретная таблица в пресетах не хранится, а других источников у планового запуска
    нет. Обязательное значение без `default`, которого нет в пресете, тоже не придёт.
    Секреты (`secret`) берутся из хранилища паролей и здесь не проверяются."""
    gap = secret_table_gap(manifest)
    if gap:
        return gap
    missing = [p for p in manifest.params
               if p.required and p.type != "secret" and p.name not in preset.params and p.default is None]
    if missing:
        names = ", ".join(f"«{p.title}»" for p in missing)
        return f"в пресете «{preset.name}» нет обязательных значений: {names}. Сохраните пресет заново с ними"
    return None


class CronTab:
    """crontab пользователя. Читается целиком, свои строки меняются, чужие остаются как были."""

    kind = "cron"

    def __init__(self, command: Sequence[str] | None = None):
        self.command = list(command) if command else _command(ENV_CRONTAB, "crontab")

    def _read(self) -> list[str]:
        p = _call([*self.command, "-l"], "crontab")
        if p.returncode != 0:
            err = _text(p.stderr).strip()
            if "no crontab" in err.lower():
                return []
            raise ReplayError("scheduler_error", f"crontab -l: {err or f'код {p.returncode}'}")
        return _text(p.stdout).splitlines()

    def _write(self, lines: list[str]) -> None:
        p = _call([*self.command, "-"], "crontab", stdin="".join(f"{x}\n" for x in lines))
        if p.returncode != 0:
            raise ReplayError("scheduler_error", f"crontab не принял расписание: {_text(p.stderr).strip() or p.returncode}")

    @staticmethod
    def _owner(line: str) -> str | None:
        m = _MARK_RE.search(line)
        return m.group(1) if m and not line.lstrip().startswith("#") else None

    def installed(self) -> dict[str, str]:
        return {sid: line for line in self._read() if (sid := self._owner(line))}

    def line(self, plan: TaskPlan) -> str:
        path = ":".join(dict.fromkeys([*plan.path_dirs, "/usr/local/bin", "/usr/bin", "/bin"]))
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in {"PATH": path, **plan.env}.items())
        cmd = " ".join(shlex.quote(a) for a in plan.argv)
        text = f"{env} {cmd} >> {shlex.quote(str(plan.log))} 2>&1"
        # В crontab % значит перевод строки, буквальный нужно экранировать.
        return f"{plan.spec.cron_expr()} {text.replace('%', chr(92) + '%')} # {MARKER}{plan.id}"

    def install(self, plan: TaskPlan) -> None:
        lines = [x for x in self._read() if self._owner(x) != plan.id]
        self._write([*lines, self.line(plan)])

    def remove(self, sid: str) -> None:
        lines = self._read()
        kept = [x for x in lines if self._owner(x) != sid]
        if kept != lines:
            self._write(kept)


class Schtasks:
    """Планировщик задач Windows: задачи `dawg-replay\\<id>` запускают обёртку `<id>.cmd`."""

    kind = "schtasks"

    def __init__(self, command: Sequence[str] | None = None):
        self.command = list(command) if command else _command(ENV_SCHTASKS, "schtasks")

    @staticmethod
    def task_name(sid: str) -> str:
        return f"{TASK_FOLDER}\\{sid}"

    def installed(self) -> dict[str, str]:
        p = _call([*self.command, "/Query", "/FO", "CSV", "/NH"], "schtasks")
        if p.returncode != 0:
            raise ReplayError("scheduler_error", f"schtasks /Query: {_text(p.stderr).strip() or p.returncode}")
        out: dict[str, str] = {}
        prefix = f"\\{TASK_FOLDER}\\"
        for row in csv.reader(io.StringIO(_text(p.stdout))):
            if row and row[0].startswith(prefix):
                sid = row[0][len(prefix):]
                if _ID_RE.match(sid):
                    out[sid] = row[0]
        return out

    @staticmethod
    def wrapper(plan: TaskPlan) -> str:
        def q(s: str) -> str:
            return '"' + s.replace("%", "%%") + '"'

        lines = ["@echo off", "chcp 65001 >nul", f'set "PATH={";".join(plan.path_dirs).replace("%", "%%")};%PATH%"']
        lines += [f'set "{k}={v.replace("%", "%%")}"' for k, v in plan.env.items()]
        program, *rest = plan.argv
        lines.append(" ".join([q(program), *(_cmd_arg(a) for a in rest)]) + f" >> {q(str(plan.log))} 2>&1")
        return "\r\n".join(lines) + "\r\n"

    @staticmethod
    def schedule_args(spec: Spec) -> list[str]:
        if spec.kind == "daily":
            return ["/SC", "DAILY", "/ST", spec.time or "00:00"]
        if spec.kind == "weekly":
            return ["/SC", "WEEKLY", "/D", WEEKDAYS[spec.weekday or 0].upper(), "/ST", spec.time or "00:00"]
        if spec.kind == "hours":
            return ["/SC", "HOURLY", "/MO", str(spec.hours), "/ST", "00:00"]
        raise ReplayError("schedule_unsupported", "выражения cron в Планировщике задач Windows не поддерживаются: "
                          "используйте --daily, --weekly или --every-hours")

    def install(self, plan: TaskPlan) -> None:
        args = self.schedule_args(plan.spec)
        plan.script_file.parent.mkdir(parents=True, exist_ok=True)
        plan.script_file.write_text(self.wrapper(plan), encoding="utf-8", newline="")
        p = _call([*self.command, "/Create", "/F", "/TN", self.task_name(plan.id), "/TR", f'"{plan.script_file}"', *args],
                  "schtasks")
        if p.returncode != 0:
            raise ReplayError("scheduler_error", f"schtasks /Create: {_text(p.stderr).strip() or p.returncode}")

    def remove(self, sid: str) -> None:
        p = _call([*self.command, "/Delete", "/F", "/TN", self.task_name(sid)], "schtasks")
        if p.returncode != 0 and sid in self.installed():
            raise ReplayError("scheduler_error", f"schtasks /Delete: {_text(p.stderr).strip() or p.returncode}")


def default_backend() -> Backend:
    kind = os.environ.get(ENV_SCHEDULER) or ("schtasks" if sys.platform == "win32" else "cron")
    return Schtasks() if kind == "schtasks" else CronTab()


def self_command() -> list[str]:
    """Полный путь к dawg-replay того же окружения, что и этот процесс.

    У `uv tool install` это `<каталог инструмента>/bin/dawg-replay`: путь не меняется
    при `uv tool upgrade`, поэтому задачи переживают обновление.
    """
    bindir = Path(sys.executable).parent
    for name in ("dawg-replay.exe", "dawg-replay") if sys.platform == "win32" else ("dawg-replay",):
        if (bindir / name).is_file():
            return [str(bindir / name)]
    found = shutil.which("dawg-replay")
    if found:
        return [str(Path(found).absolute())]
    return [sys.executable, "-m", "dawg_replay"]


class Schedules:
    def __init__(self, app: Any, backend: Backend | None = None):
        self.app = app
        self.backend = backend if backend is not None else default_backend()
        self._db: sqlite3.Connection = app.db

    # --- чтение ---

    def get(self, sid: str) -> Schedule:
        row = self._db.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
        if row is None:
            raise ReplayError("schedule_not_found", f"расписания `{sid}` нет; список: dawg-replay schedule list")
        return _schedule(row)

    def all(self, script: str | None = None) -> list[Schedule]:
        if script is None:
            rows = self._db.execute("SELECT * FROM schedules ORDER BY script_name, created_at")
        else:
            rows = self._db.execute("SELECT * FROM schedules WHERE script_name = ? ORDER BY created_at", (script,))
        return [_schedule(r) for r in rows]

    def list(self, script: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        """Расписания со сверкой: state ok или missing; orphans: задачи без записи в приложении."""
        items = self.all(script)
        try:
            installed = self.backend.installed()
            error = None
        except ReplayError as e:
            installed, error = None, e.message
        out = []
        for s in items:
            d = s.to_dict(now)
            d["state"] = "unknown" if installed is None else ("ok" if s.id in installed else "missing")
            d["log"] = str(self.log_path(s.id))
            out.append(d)
        known = {s.id for s in self.all()}
        orphans = [] if installed is None or script is not None else [
            {"id": sid, "entry": entry} for sid, entry in sorted(installed.items()) if sid not in known
        ]
        return {"scheduler": self.backend.kind, "schedules": out, "orphans": orphans, "error": error}

    def upcoming(self, limit: int = 5, now: datetime | None = None) -> list[dict[str, Any]]:
        """Ближайшие запуски по всем расписаниям (без обращения к планировщику)."""
        now = now or datetime.now()
        items = []
        for s in self.all():
            nxt = next_run(s.spec.cron_expr(), now)
            if nxt is not None:
                items.append((nxt, s))
        items.sort(key=lambda x: (x[0], x[1].script))
        return [{**s.to_dict(now), "at": t} for t, s in items[:limit]]

    # --- изменения ---

    def add(self, script: str, preset: str, spec: Spec) -> Schedule:
        self.app.registry.get(script)
        p = self.app.get_preset(script, preset)  # пресет должен быть
        preset = p.name
        problem = preset_gap(self.app.registry.manifest(script), p)
        if problem:
            raise ReplayError("schedule_impossible", problem)
        if self.backend.kind == "schtasks":
            Schtasks.schedule_args(spec)  # cron на Windows: отказ до записи
        sid = secrets.token_hex(4)
        self._db.execute(
            "INSERT INTO schedules (id, script_name, preset, spec_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (sid, script, preset, json.dumps(spec.to_dict()), utcnow()),
        )
        try:
            self.backend.install(self.plan(self.get(sid)))
        except BaseException:
            self._db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
            self._cleanup_files(sid)
            raise
        return self.get(sid)

    def remove(self, sid: str) -> dict[str, Any]:
        """Снимает задачу и удаляет запись. Сироту (задача без записи) тоже снимает."""
        known = self._db.execute("SELECT 1 FROM schedules WHERE id = ?", (sid,)).fetchone() is not None
        if not known and sid not in self.backend.installed():
            raise ReplayError("schedule_not_found", f"расписания `{sid}` нет ни в приложении, ни в планировщике")
        self.backend.remove(sid)
        self._db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
        self._cleanup_files(sid)
        return {"id": sid, "was_orphan": not known}

    def remove_for(self, script: str, preset: str | None = None) -> list[str]:
        """Снимает расписания скрипта (или одного его пресета). Возвращает их id."""
        ids = [s.id for s in self.all(script) if preset is None or s.preset == preset]
        for sid in ids:
            self.remove(sid)
        return ids

    def sync(self) -> dict[str, Any]:
        """Ставит пропавшие задачи заново и снимает сирот."""
        installed = self.backend.installed()
        reinstalled = []
        for s in self.all():
            if s.id not in installed:
                self.backend.install(self.plan(s))
                reinstalled.append(s.id)
        known = {s.id for s in self.all()}
        removed = [sid for sid in installed if sid not in known]
        for sid in removed:
            self.backend.remove(sid)
            self._cleanup_files(sid)
        return {"reinstalled": reinstalled, "orphans_removed": removed}

    # --- задача ---

    def log_path(self, sid: str) -> Path:
        return self.app.paths.schedules / f"{sid}.log"

    def plan(self, s: Schedule) -> TaskPlan:
        uv = shutil.which("uv")
        if not uv:
            raise ReplayError("uv_not_found", "uv не найден в PATH: запуск по расписанию его не найдёт. "
                              "Установите uv: https://docs.astral.sh/uv/getting-started/installation/")
        argv = [*self_command(), "run", s.script, "--preset", s.preset, "--follow", "--schedule", s.id]
        dirs = list(dict.fromkeys([str(Path(uv).absolute().parent), str(Path(argv[0]).parent)]))
        self.app.paths.schedules.mkdir(parents=True, exist_ok=True)
        return TaskPlan(
            id=s.id,
            spec=s.spec,
            argv=argv,
            path_dirs=dirs,
            env={"DAWG_REPLAY_HOME": str(self.app.paths.home.absolute())},
            log=self.log_path(s.id),
            script_file=self.app.paths.schedules / f"{s.id}.cmd",
        )

    def _cleanup_files(self, sid: str) -> None:
        for suffix in (".cmd", ".log"):
            try:
                (self.app.paths.schedules / f"{sid}{suffix}").unlink()
            except OSError:
                pass


def _schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(row["id"], row["script_name"], row["preset"], Spec.from_dict(json.loads(row["spec_json"])), row["created_at"])
