"""Страховка от утечки браузеров DAWG после запуска (ТЗ 5.6).

После завершения процесса с любым исходом:

1. по `events.jsonl` находятся сессии с `open` без `close`, они освобождаются
   через dawg-sdk-python;
2. если есть `provisioning` без `open` и без `failed` (процесс убит между POST
   и ответом), id браузера неизвестен. Освобождать все сессии ключа нельзя:
   на нём живут `dawg-mcp` и другие запуски. Приложение ищет сессии ключа,
   созданные в окне вокруг `provisioning`, и предупреждает о них. Освобождает
   их пользователь вручную (`release_session`).

Результат дописывается в `events.jsonl` с `source = "dawg-replay"`: `session close`
за каждый освобождённый браузер и строки `log`; и в `insurance.json` в каталоге запуска.
Повторный вызов ничего не делает: освобождённые сессии уже закрыты событием.

Подключение к раннеру:

    ScriptRun(..., after_exit=[insurance.hook(api_key, base_url)])
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from dawg_replay_kit.dawg_api import DEFAULT_BASE_URL, HTTP_TIMEOUT, Sessions
from dawg_replay_kit.events import now_iso

REPORT_FILE = "insurance.json"
SOURCE = "dawg-replay"

# Окно поиска сессий для provisioning без open. Сессия создаётся на сервере
# между отправкой POST и ответом (таймаут POST в kit 30 с). Запас на расхождение часов.
WINDOW_BEFORE = timedelta(seconds=15)
WINDOW_AFTER = timedelta(seconds=90)

# Если процесс оборвался во время POST, платформа может доделать браузер уже после
# его смерти. Перед поиском suspects ждём, пока POST гарантированно закончился бы:
# provisioning + таймаут POST в kit + запас. Не дольше этого срока от текущего момента
# (защита от странных ts в событиях).
POST_TIMEOUT = timedelta(seconds=HTTP_TIMEOUT)
POST_MARGIN = timedelta(seconds=5)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# wait(seconds) -> True, если подождали полностью; False, если ожидание прервали.
Wait = Callable[[float], bool]


def event_wait(stop: threading.Event | None) -> Wait:
    """Ожидание, которое прерывается событием `stop` (отмена запуска, выход приложения)."""
    stop = stop if stop is not None else threading.Event()
    return lambda seconds: not stop.wait(seconds)


class SessionsApi(Protocol):
    def list(self) -> list[dict[str, Any]]: ...

    def release(self, session_id: str) -> bool: ...

    def close(self) -> None: ...


@dataclass
class Scan:
    unclosed: dict[str, str | None] = field(default_factory=dict)  # session_id -> browser_id
    known: set[str] = field(default_factory=set)  # все session_id этого запуска
    pending: list[str] = field(default_factory=list)  # ts provisioning без open и без failed


@dataclass
class Report:
    released: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)  # session_id, error
    suspects: list[dict[str, Any]] = field(default_factory=list)  # сессии ключа из окна provisioning
    pending_provisioning: list[str] = field(default_factory=list)
    error: str | None = None
    waited_s: float = 0.0  # сколько ждали окончания POST перед поиском suspects
    wait_interrupted: bool = False

    @property
    def clean(self) -> bool:
        return not (self.failed or self.suspects or self.error)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def scan_events(path: Path) -> Scan:
    """Читает события `session`. Битые строки пропускаются."""
    scan = Scan()
    if not path.exists():
        return scan
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict) or e.get("t") != "session":
                continue
            action, sid = e.get("action"), e.get("session_id")
            if action == "provisioning":
                scan.pending.append(str(e.get("ts", "")))
            elif action == "open" and sid:
                # Создания идут по очереди на браузер: open закрывает самый старый provisioning.
                if scan.pending:
                    scan.pending.pop(0)
                scan.unclosed[str(sid)] = e.get("browser_id")
                scan.known.add(str(sid))
            elif action == "failed":
                if scan.pending:
                    scan.pending.pop(0)
            elif action == "close" and sid:
                scan.unclosed.pop(str(sid), None)
                scan.known.add(str(sid))
    return scan


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_suspects(
    sessions: list[dict[str, Any]], pending: list[str], known: set[str], finished_at: str | None
) -> list[dict[str, Any]]:
    """Сессии ключа, созданные в окне вокруг provisioning без open."""
    end_cap = _parse_ts(finished_at)
    windows = []
    for ts in pending:
        start = _parse_ts(ts)
        if start is None:
            continue
        end = start + WINDOW_AFTER
        if end_cap is not None:
            end = min(end, end_cap + WINDOW_BEFORE)
        windows.append((start - WINDOW_BEFORE, end))
    out = []
    for s in sessions:
        sid = str(s.get("id", ""))
        started = _parse_ts(s.get("startedAt"))
        if not sid or sid in known or started is None:
            continue
        if any(a <= started <= b for a, b in windows):
            out.append(
                {
                    "session_id": sid,
                    "browser_id": s.get("browserInstanceId"),
                    "status": s.get("status"),
                    "started_at": s.get("startedAt"),
                }
            )
    return out


def post_wait_seconds(pending: list[str], now: datetime) -> float:
    """Сколько ждать перед поиском suspects: до последнего provisioning + таймаут POST + запас."""
    starts = [t for t in (_parse_ts(ts) for ts in pending) if t is not None]
    if not starts:
        return 0.0
    limit = POST_TIMEOUT + POST_MARGIN
    until = max(starts) + limit
    return max(0.0, min((until - now).total_seconds(), limit.total_seconds()))


def insure(
    run_dir: Path,
    api: Callable[[], SessionsApi] | None,
    finished_at: str | None = None,
    *,
    now: Callable[[], datetime] | None = None,
    wait: Wait | None = None,
) -> Report:
    """Освобождает браузеры запуска по его событиям. Пишет итог в лог запуска и insurance.json.

    `api` создаёт клиента только если есть что делать; None значит, что ключа DAWG нет.
    Сессии с `open` без `close` освобождаются сразу. Только если есть `provisioning`
    без `open` и `failed`, перед списком сессий ключа страховка ждёт окончания POST
    (`post_wait_seconds`). `wait` прерываемый: при отмене или выходе приложения
    список берётся сразу, итог тот же, в отчёте `wait_interrupted`.
    """
    now = now if now is not None else _utcnow
    wait = wait if wait is not None else event_wait(None)
    run_dir = Path(run_dir)
    events_path = run_dir / "events.jsonl"
    scan = scan_events(events_path)
    report = Report(pending_provisioning=list(scan.pending))
    if not scan.unclosed and not scan.pending:
        return report

    lines: list[tuple[str, str]] = []
    closed: list[tuple[str, str | None]] = []
    client = None
    no_key = api is None
    if not no_key:
        try:
            client = api()
        except MissingKey:
            no_key = True
        except Exception as e:
            report.error = f"страховка не сработала: {e}"
            report.failed = [{"session_id": sid, "error": str(e)} for sid in scan.unclosed]
            lines.append(("error", report.error))
    if no_key:
        report.error = "ключа DAWG нет, браузеры запуска не проверены"
        report.failed = [{"session_id": sid, "error": "нет ключа DAWG"} for sid in scan.unclosed]
        lines.append(("error", f"страховка: {report.error}; незакрытые сессии: {', '.join(scan.unclosed) or 'нет'}"))
    elif client is not None:
        try:
            for sid in scan.unclosed:
                try:
                    if client.release(sid):
                        report.released.append(sid)
                        closed.append((sid, scan.unclosed[sid]))
                        lines.append(("info", f"страховка: освобождён браузер {sid}, скрипт не закрыл его сам"))
                    else:
                        report.failed.append({"session_id": sid, "error": "сессия осталась в списке ключа"})
                        lines.append(("error", f"страховка: браузер {sid} не освобождён, он всё ещё в списке сессий"))
                except Exception as e:
                    report.failed.append({"session_id": sid, "error": str(e)})
                    lines.append(("error", f"страховка: браузер {sid} не освобождён: {e}"))
            if scan.pending:
                delay = post_wait_seconds(scan.pending, now())
                if delay > 0:
                    started = now()
                    report.wait_interrupted = not wait(delay)
                    report.waited_s = round(max(0.0, (now() - started).total_seconds()), 3)
                    if report.wait_interrupted:
                        lines.append(
                            (
                                "warn",
                                "страховка: ожидание окончания создания браузера прервано, "
                                "список сессий ключа может быть неполным",
                            )
                        )
                try:
                    report.suspects = find_suspects(client.list(), scan.pending, scan.known, finished_at)
                except Exception as e:
                    report.error = f"не удалось получить список сессий ключа: {e}"
                    lines.append(("error", f"страховка: {report.error}"))
        except Exception as e:
            report.error = f"страховка не сработала: {e}"
            report.failed = [{"session_id": sid, "error": str(e)} for sid in scan.unclosed if sid not in report.released]
            lines.append(("error", report.error))
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    if scan.pending and report.error is None:
        if report.suspects:
            ids = ", ".join(s["session_id"] for s in report.suspects)
            lines.append(
                (
                    "warn",
                    "возможно, остался браузер: процесс завершился во время создания браузера. "
                    f"Сессии ключа, созданные в это время: {ids}. Освободите их вручную, если они не нужны",
                )
            )
        else:
            lines.append(("info", "страховка: процесс завершился во время создания браузера, новых сессий ключа не найдено"))

    _append(events_path, lines, closed)
    (run_dir / REPORT_FILE).write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _append(path: Path, lines: list[tuple[str, str]], closed: list[tuple[str, str | None]]) -> None:
    """Дописывает итог в лог запуска. `session close` за освобождённые браузеры делает
    повторную страховку безвредной и поправляет счётчик браузеров в UI."""
    if not lines and not closed:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for sid, bid in closed:
            e = {"t": "session", "ts": now_iso(), "action": "close", "session_id": sid, "source": SOURCE}
            if bid:
                e["browser_id"] = bid
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        for level, msg in lines:
            f.write(json.dumps({"t": "log", "ts": now_iso(), "level": level, "msg": msg, "source": SOURCE}, ensure_ascii=False) + "\n")


class MissingKey(Exception):
    """Ключа DAWG нет: проверить браузеры нечем."""


def hook(
    api_key: str | Callable[[], str | None] | None,
    base_url: str | Callable[[], str | None] | None = None,
    sessions: Callable[[], SessionsApi] | None = None,
    stop: threading.Event | None = None,
):
    """Хук after_exit. Ключ и base_url можно передать функциями: тогда они читаются
    (keyring, config.toml) только если страховке есть что делать. `stop` прерывает
    ожидание окончания POST (отмена запуска, выход приложения)."""

    def factory() -> SessionsApi:
        if sessions is not None:
            return sessions()
        key = api_key() if callable(api_key) else api_key
        if not key:
            raise MissingKey()
        url = base_url() if callable(base_url) else base_url
        return Sessions(key, url or DEFAULT_BASE_URL)

    def insurance(outcome: Any) -> None:
        insure(
            outcome.run_dir,
            factory if (sessions is not None or api_key) else None,
            getattr(outcome, "finished_at", None),
            wait=event_wait(stop),
        )

    return insurance


def release_session(api_key: str, session_id: str, base_url: str | None = None) -> bool:
    """Ручное освобождение подозрительной сессии (кнопка в UI, команда CLI)."""
    s = Sessions(api_key, base_url or DEFAULT_BASE_URL)
    try:
        return s.release(session_id)
    finally:
        s.close()


# --- остаточное окно: ручное освобождение (CLI `release`, кнопка UI) ---------------

WARNING_TEXT = "возможно, остался браузер"


@dataclass
class SuspectRelease:
    session_id: str
    released: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class NotASuspect(ValueError):
    """Сессия не из списка подозрительных этого запуска."""


def load_report(run_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((Path(run_dir) / REPORT_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def pending_suspects(run_dir: Path) -> list[dict[str, Any]]:
    """Подозрительные сессии запуска, которые ещё не освобождены вручную."""
    report = load_report(run_dir) or {}
    return [s for s in report.get("suspects") or [] if isinstance(s, dict) and not s.get("released")]


def warning_line(suspects: list[dict[str, Any]]) -> str:
    ids = ", ".join(s["session_id"] for s in suspects)
    return f"{WARNING_TEXT}: процесс завершился во время создания браузера. Сессии ключа, созданные в это время: {ids}"


def release_suspects(
    run_dir: Path,
    session_ids: list[str] | None,
    release: Callable[[str], bool],
) -> list[SuspectRelease]:
    """Освобождает подозрительные сессии запуска. None: все ещё не освобождённые.

    Освободить можно только сессию из `suspects` этого запуска. Итог пишется в
    `insurance.json` (у сессии `released`, `released_at` или `release_error`) и строками
    `log` в `events.jsonl`. `session close` не пишется: этот запуск сессию не открывал.
    """
    run_dir = Path(run_dir)
    report = load_report(run_dir) or {}
    suspects = [s for s in report.get("suspects") or [] if isinstance(s, dict)]
    by_id = {s.get("session_id"): s for s in suspects}
    pending = [s["session_id"] for s in suspects if not s.get("released")]
    if session_ids is None:
        targets = pending
    else:
        unknown = [sid for sid in session_ids if sid not in by_id]
        if unknown:
            have = ", ".join(pending) or "нет"
            raise NotASuspect(
                f"сессии {', '.join(unknown)} нет среди подозрительных сессий запуска; можно освободить: {have}"
            )
        targets = [sid for sid in dict.fromkeys(session_ids) if not by_id[sid].get("released")]

    results: list[SuspectRelease] = []
    lines: list[tuple[str, str]] = []
    for sid in targets:
        try:
            ok = release(sid)
            err = None if ok else "сессия осталась в списке ключа"
        except Exception as e:
            ok, err = False, str(e)
        results.append(SuspectRelease(sid, ok, err))
        entry = by_id[sid]
        if ok:
            entry["released"] = True
            entry["released_at"] = now_iso()
            entry.pop("release_error", None)
            lines.append(("info", f"остаточное окно: сессия {sid} освобождена вручную"))
        else:
            entry["release_error"] = err
            lines.append(("error", f"остаточное окно: сессия {sid} не освобождена: {err}"))

    if results:
        report["suspects"] = suspects
        (run_dir / REPORT_FILE).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        _append(run_dir / "events.jsonl", lines, [])
    return results
