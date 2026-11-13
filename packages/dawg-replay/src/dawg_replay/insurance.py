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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from dawg_replay_kit.dawg_api import DEFAULT_BASE_URL, Sessions
from dawg_replay_kit.events import now_iso

REPORT_FILE = "insurance.json"
SOURCE = "dawg-replay"

# Окно поиска сессий для provisioning без open. Сессия создаётся на сервере
# между отправкой POST и ответом (таймаут POST в kit 30 с). Запас на расхождение часов.
WINDOW_BEFORE = timedelta(seconds=15)
WINDOW_AFTER = timedelta(seconds=90)


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


def insure(run_dir: Path, api: Callable[[], SessionsApi] | None, finished_at: str | None = None) -> Report:
    """Освобождает браузеры запуска по его событиям. Пишет итог в лог запуска и insurance.json.

    `api` создаёт клиента только если есть что делать; None значит, что ключа DAWG нет.
    """
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
):
    """Хук after_exit. Ключ и base_url можно передать функциями: тогда они читаются
    (keyring, config.toml) только если страховке есть что делать."""

    def factory() -> SessionsApi:
        if sessions is not None:
            return sessions()
        key = api_key() if callable(api_key) else api_key
        if not key:
            raise MissingKey()
        url = base_url() if callable(base_url) else base_url
        return Sessions(key, url or DEFAULT_BASE_URL)

    def insurance(outcome: Any) -> None:
        insure(outcome.run_dir, factory if (sessions is not None or api_key) else None, getattr(outcome, "finished_at", None))

    return insurance


def release_session(api_key: str, session_id: str, base_url: str | None = None) -> bool:
    """Ручное освобождение подозрительной сессии (кнопка в UI, команда CLI)."""
    s = Sessions(api_key, base_url or DEFAULT_BASE_URL)
    try:
        return s.release(session_id)
    finally:
        s.close()
