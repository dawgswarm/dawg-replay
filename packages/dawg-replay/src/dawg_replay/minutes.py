"""Минуты браузеров DAWG по запускам: оценка по событиям `session` в events.jsonl.

Сессия считается от `open` до `close` того же `session_id`. Если `close` нет
(скрипт убит, страховка не дописала закрытие), конец берётся по концу запуска,
для идущего запуска по текущему моменту. Время между `provisioning` и `open`
не считается: id браузера ещё неизвестен.

Это оценка: биллинг DAWG считает по своим часам и правилам округления, поэтому
в интерфейсе число подписано «примерно», а рядом показывается точный остаток
из личного кабинета (`account.fetch_usage`).

Для завершённого запуска результат кэшируется в `minutes.json` в каталоге
запуска и пересчитывается, если файл событий изменился (страховка дописывает
закрытия после конца процесса).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CACHE_FILE = "minutes.json"
CACHE_VERSION = 1
# Быстрый отсев строк без событий сессии: в больших файлах почти всё это rows и progress.
_SESSION_MARK = b'"session"'


@dataclass
class RunMinutes:
    seconds: float = 0.0
    sessions: int = 0
    unclosed: int = 0  # сессий без close, их конец взят по концу запуска

    @property
    def minutes(self) -> float:
        return self.seconds / 60

    def to_dict(self) -> dict[str, Any]:
        return {"minutes": round(self.minutes, 2), "sessions": self.sessions, "unclosed": self.unclosed}


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def count(events_path: Path, end: datetime | None) -> RunMinutes:
    """Минуты по файлу событий. `end`: конец запуска для сессий без close."""
    opened: dict[str, datetime] = {}
    spans: dict[str, tuple[datetime, datetime | None]] = {}
    try:
        f = open(events_path, "rb")
    except OSError:
        return RunMinutes()
    with f:
        for raw in f:
            if _SESSION_MARK not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("t") != "session" or not e.get("session_id"):
                continue
            sid, action, ts = str(e["session_id"]), e.get("action"), _ts(e.get("ts"))
            if ts is None:
                continue
            if action == "open" and sid not in spans:
                opened[sid] = ts
                spans[sid] = (ts, None)
            elif action == "close" and sid in opened and spans[sid][1] is None:
                spans[sid] = (opened[sid], ts)
    out = RunMinutes(sessions=len(spans))
    for start, stop in spans.values():
        if stop is None:
            out.unclosed += 1
            stop = end
        if stop is not None and stop > start:
            out.seconds += (stop - start).total_seconds()
    return out


def for_run(run_dir: Path, rec: Any, now: datetime | None = None) -> RunMinutes:
    """Минуты запуска `rec` (запись из RunStore). Для завершённого берётся кэш, если файл событий не менялся."""
    events = run_dir / "events.jsonl"
    finished = _ts(rec.finished_at)
    if finished is None:
        return count(events, now or datetime.now(timezone.utc))
    try:
        st = events.stat()
        stamp = [st.st_size, st.st_mtime_ns]
    except OSError:
        stamp = None
    cache = run_dir / CACHE_FILE
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("v") == CACHE_VERSION and data.get("stamp") == stamp:
            return RunMinutes(float(data["seconds"]), int(data["sessions"]), int(data["unclosed"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    result = count(events, finished)
    if run_dir.is_dir():
        tmp = cache.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"v": CACHE_VERSION, "stamp": stamp, "seconds": result.seconds,
                                       "sessions": result.sessions, "unclosed": result.unclosed}), encoding="utf-8")
            os.replace(tmp, cache)
        except OSError:
            pass
    return result


@dataclass
class ScriptMinutes:
    script: str
    minutes: float = 0.0
    runs: int = 0  # запусков за период
    with_browsers: int = 0  # из них открывали браузер

    def to_dict(self) -> dict[str, Any]:
        return {"script": self.script, "minutes": round(self.minutes, 2), "runs": self.runs, "with_browsers": self.with_browsers}


@dataclass
class Period:
    days: int
    since: str
    script: str | None
    minutes: float = 0.0
    runs: int = 0
    scripts: list[ScriptMinutes] = field(default_factory=list)  # по убыванию минут
    by_run: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "since": self.since,
            "script": self.script,
            "minutes": round(self.minutes, 2),
            "runs": self.runs,
            "approximate": True,
            "scripts": [s.to_dict() for s in self.scripts],
            "by_run": self.by_run,
        }


def period(app: Any, days: int = 7, script: str | None = None, now: datetime | None = None) -> Period:
    """Минуты за последние `days` дней по запускам, созданным в этот период, всего и по скриптам."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    out = Period(days=days, since=since, script=script)
    per: dict[str, ScriptMinutes] = {}
    for rec in app.runs.created_since(since, script):
        m = for_run(app.paths.run_dir(rec.id), rec, now)
        s = per.setdefault(rec.script_name, ScriptMinutes(rec.script_name))
        s.runs += 1
        s.minutes += m.minutes
        if m.sessions:
            s.with_browsers += 1
        out.runs += 1
        out.minutes += m.minutes
        out.by_run.append({"run": rec.id, "script": rec.script_name, "created_at": rec.created_at,
                           "label": rec.label, **m.to_dict()})
    out.scripts = sorted(per.values(), key=lambda s: (-s.minutes, s.script))
    return out


def fmt(minutes: float) -> str:
    """Короткая запись для интерфейса и CLI: `≈ 12 мин`, `≈ 0,4 мин`, `меньше 0,1 мин`."""
    if minutes <= 0:
        return "0 мин"
    if minutes < 0.1:
        return "меньше 0,1 мин"
    if minutes < 10:
        return f"≈ {minutes:.1f} мин".replace(".", ",")
    return f"≈ {round(minutes)} мин"
