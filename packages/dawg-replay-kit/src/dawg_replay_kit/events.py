"""События запуска (docs/contract.md, раздел 4).

В режиме приложения события пишутся в файл JSON Lines из `DAWG_SCRIPT_EVENTS`,
в автономном режиме печатаются в stderr в читаемом виде.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

LOG_LEVELS = ("debug", "info", "warn", "error")
RESULT_STATUSES = ("ok", "partial", "failed")
SESSION_ACTIONS = ("provisioning", "open", "close", "failed")
ITEM_STATUSES = ("ok", "failed", "skipped")
ITEM_MSG_MAX = 1000  # длинный текст исключения обрезается: строка отчёта, не трейсбек
THROTTLE_SECONDS = 0.25  # не чаще 4 раз в секунду на id
MASK = "***"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Sink(Protocol):
    def write(self, event: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


def _dumps(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))


class JsonlSink:
    """Файл событий. Каждая строка дописывается и сбрасывается на диск сразу."""

    def __init__(self, path: str | Path):
        self._f = open(path, "a", encoding="utf-8", newline="\n")

    def write(self, event: dict[str, Any]) -> None:
        self._f.write(_dumps(event) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class ConsoleSink:
    """Читаемый вывод для автономного режима."""

    def __init__(self, stream: TextIO | None = None, output_dir: Path | None = None):
        self._stream = stream
        self._output_dir = output_dir

    @property
    def stream(self) -> TextIO:
        if self._stream is not None:
            return self._stream
        import sys

        return sys.stderr

    def write(self, event: dict[str, Any]) -> None:
        print(self.format(event), file=self.stream, flush=True)

    def format(self, e: dict[str, Any]) -> str:
        ts = e.get("ts", "")
        clock = ts[11:19] if len(ts) >= 19 else ts
        t = e.get("t")
        if t == "log":
            body = f"{e['level'].upper():5} {e['msg']}"
        elif t == "stage":
            body = f"==> {e['name']}"
        elif t == "progress":
            total = f"/{e['total']}" if e.get("total") is not None else ""
            body = f"{e['label']}: {e['done']}{total}"
        elif t == "metric":
            unit = f" {e['unit']}" if e.get("unit") else ""
            body = f"{e['label']}: {e['value']}{unit}"
        elif t == "rows":
            body = f"таблица {e['table']}: +{len(e['rows'])} строк"
            if e["rows"]:
                body += f", первая: {_dumps(e['rows'][0])}"
        elif t == "artifact":
            where = self._output_dir / e["path"] if self._output_dir else e["path"]
            body = f"файл «{e['label']}»: {where}"
        elif t == "item":
            msg = f": {e['msg']}" if e.get("msg") else ""
            if e.get("row") is not None and str(e["row"]) == str(e["key"]):
                label = f"строка {e['row']}"  # ключ и есть номер строки: без «2 (строка 2)»
            else:
                label = str(e["key"]) + (f" (строка {e['row']})" if e.get("row") is not None else "")
            body = f"{e['status']:7} {label}{msg}"
        elif t == "session":
            sid = f" {e['session_id']}" if e.get("session_id") else ""
            body = f"браузер {e['action']}{sid}"
        elif t == "result":
            body = f"ИТОГ {e['status']}: {e['summary']}"
            if e.get("count") is not None:
                body += f" (записей: {e['count']})"
            if e.get("main"):
                where = self._output_dir / e["main"] if self._output_dir else e["main"]
                body += f"\n           главный файл: {where}"
            for link in e.get("links") or []:
                body += f"\n           ссылка «{link.get('title')}»: {link.get('url')}"
        elif t == "ask":
            body = f"ВОПРОС: {e['question']}"
            if e.get("choices"):
                body += " (" + " / ".join(str(c) for c in e["choices"]) + ")"
            elif e.get("kind") == "confirm":
                body += " (да / нет)"
            deadline = str(e.get("deadline") or "")
            body += f", ответить до {deadline[11:16] if len(deadline) >= 16 else deadline}"
        elif t == "ask_closed":
            if e.get("outcome") == "answered":
                answer = e.get("answer")
                shown = ("да" if answer else "нет") if isinstance(answer, bool) else answer
                body = "ответ получен" + (f": {shown}" if "answer" in e else "")
            else:
                body = "ответа на вопрос не дождались"
        else:
            body = _dumps(e)
        return f"[{clock}] {body}"

    def close(self) -> None:
        pass


def _mask(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for s in secrets:
            value = value.replace(s, MASK)
        return value
    if isinstance(value, Mapping):
        return {_mask(k, secrets): _mask(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(v, secrets) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _mask(str(value), secrets)


class Emitter:
    """Пишет события: ставит `ts`, маскирует секреты, прореживает progress и metric.

    Прореженное значение не теряется: последнее отложенное уходит само, как только
    закончится окно прореживания (фоновый поток-демон), либо раньше, со следующим
    событием или в `flush()`/`close()`. Так полоска прогресса в приложении не
    отстаёт, пока скрипт долго ждёт страницу.

    Поток один на эмиттер, запускается при первом отложенном значении и работает
    под тем же замком, что и запись, поэтому с `close()` не гоняется: после
    закрытия он ничего не пишет и завершается. `auto_flush=False` отключает поток
    (тестам с ручными часами нужна детерминированность).
    """

    def __init__(
        self,
        sink: Sink,
        secrets: Iterable[str] = (),
        output_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        throttle: float = THROTTLE_SECONDS,
        auto_flush: bool = True,
    ):
        self._sink = sink
        # Длинные секреты первыми, чтобы короткий не порвал длинный.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._output_dir = output_dir
        self._clock = clock
        self._throttle = throttle
        self._last: dict[tuple[str, str], float] = {}
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._auto_flush = auto_flush
        self._timer: threading.Thread | None = None
        self._closed = False

    # --- низкий уровень ---

    def emit(self, t: str, **fields: Any) -> None:
        """Записывает событие любого типа. Поля со значением None не пишутся."""
        event = {"t": t, "ts": now_iso(), **{k: v for k, v in fields.items() if v is not None}}
        with self._lock:
            self._flush_expired()
            self._write(event)

    def add_secret(self, value: str) -> None:
        """Ещё одно значение для маскировки, например секретный ответ на вопрос (kit.ask)."""
        if not value:
            return
        with self._lock:
            self._secrets = sorted({*self._secrets, value}, key=len, reverse=True)

    def _write(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        if self._secrets:
            event = _mask(event, self._secrets)
        self._sink.write(event)

    def _throttled(self, key: tuple[str, str], event: dict[str, Any], force: bool) -> None:
        with self._lock:
            now = self._clock()
            self._pending.pop(key, None)  # новое значение заменяет отложенное
            self._flush_expired(now)
            last = self._last.get(key)
            if force or last is None or now - last >= self._throttle:
                self._last[key] = now
                self._write(event)
            else:
                self._pending[key] = event
                self._schedule()

    def _flush_expired(self, now: float | None = None) -> None:
        if not self._pending:
            return
        now = self._clock() if now is None else now
        # То же выражение, что в _next_deadline: таймер не крутится вхолостую из-за округления.
        for key in [k for k in self._pending if self._last[k] + self._throttle <= now]:
            self._last[key] = now
            self._write(self._pending.pop(key))

    # --- таймер отложенных значений ---

    def _schedule(self) -> None:
        """Будит поток таймера (вызывается под замком), при первом вызове запускает его."""
        if not self._auto_flush or self._closed:
            return
        if self._timer is None or not self._timer.is_alive():
            self._timer = threading.Thread(target=self._timer_loop, name="dawg-replay-kit-flush", daemon=True)
            self._timer.start()
        self._wake.notify()

    def _next_deadline(self) -> float | None:
        if not self._pending:
            return None
        return min(self._last[k] for k in self._pending) + self._throttle

    def _timer_loop(self) -> None:
        with self._lock:
            while not self._closed:
                deadline = self._next_deadline()
                if deadline is None:
                    self._wake.wait()
                    continue
                delay = deadline - self._clock()
                if delay > 0:
                    self._wake.wait(delay)
                    continue
                try:
                    self._flush_expired()
                except Exception:
                    # Ошибку записи увидит основной поток на следующем событии;
                    # поток таймера не должен умирать молча и забирать с собой отложенные значения.
                    self._wake.wait(self._throttle)

    def flush(self) -> None:
        """Отправляет все отложенные progress и metric."""
        with self._lock:
            for key in list(self._pending):
                self._last[key] = self._clock()
                self._write(self._pending.pop(key))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.flush()
            self._closed = True
            self._wake.notify_all()  # поток таймера увидит _closed и выйдет
            self._sink.close()

    # --- API контракта ---

    def log(self, level: str, msg: Any) -> None:
        if level not in LOG_LEVELS:
            raise ValueError(f"уровень лога {level!r}, допустимы: {', '.join(LOG_LEVELS)}")
        self.emit("log", level=level, msg=str(msg))

    def stage(self, name: str) -> None:
        self.emit("stage", name=str(name))

    def progress(self, id: str, label: str, done: int | float, total: int | float | None = None) -> None:
        event = {"t": "progress", "ts": now_iso(), "id": str(id), "label": str(label), "done": done}
        if total is not None:
            event["total"] = total
        finished = total is not None and done >= total
        self._throttled(("progress", str(id)), event, force=finished)

    def metric(self, key: str, label: str, value: int | float | str, unit: str | None = None) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            value = str(value)
        event = {"t": "metric", "ts": now_iso(), "key": str(key), "label": str(label), "value": value}
        if unit is not None:
            event["unit"] = str(unit)
        self._throttled(("metric", str(key)), event, force=False)

    def rows(self, table: str, rows: Iterable[Mapping[str, Any]], columns: Iterable[str] | None = None) -> None:
        rows = list(rows)
        for i, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise TypeError(f"rows: строка {i} должна быть словарём, получено {type(row).__name__}")
        self.emit(
            "rows",
            table=str(table),
            columns=list(columns) if columns is not None else None,
            rows=[dict(r) for r in rows],
        )

    def artifact(self, path: str | Path, label: str, mime: str | None = None) -> None:
        rel = self._relative_to_output(Path(path))
        if mime is None:
            mime = mimetypes.guess_type(rel.name)[0]
        self.emit("artifact", path=rel.as_posix(), label=str(label), mime=mime)

    def _relative_to_output(self, path: Path) -> Path:
        if self._output_dir is None:
            raise RuntimeError("каталог результатов не задан")
        out = self._output_dir.resolve()
        full = (path if path.is_absolute() else out / path).resolve()
        try:
            rel = full.relative_to(out)
        except ValueError:
            raise ValueError(f"artifact: файл {path} лежит вне каталога результатов {out}") from None
        if not full.is_file():
            raise FileNotFoundError(f"artifact: файла нет: {full}")
        return rel

    def result(
        self,
        status: str,
        summary: str,
        count: int | None = None,
        links: Iterable[Mapping[str, Any]] | None = None,
        main: str | Path | None = None,
    ) -> None:
        """Итог запуска. Необязательно (контракт 1.3): `count` записей или строк, `links` список
        {"title", "url"} с адресами http(s), `main` главный файл результата в каталоге output."""
        if status not in RESULT_STATUSES:
            raise ValueError(f"статус {status!r}, допустимы: {', '.join(RESULT_STATUSES)}")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
            raise ValueError(f"result: count должен быть целым не меньше 0, получено {count!r}")
        link_list = None
        if links is not None:
            link_list = []
            for i, link in enumerate(links):
                if not isinstance(link, Mapping) or not isinstance(link.get("url"), str):
                    raise ValueError(f"result: links[{i}] должен быть словарём с title и url")
                url = link["url"].strip()
                if not url.lower().startswith(("http://", "https://")):
                    raise ValueError(f"result: links[{i}].url должен начинаться с http:// или https://")
                link_list.append({"title": str(link.get("title") or url), "url": url})
        main_path = self._relative_to_output(Path(main)).as_posix() if main is not None else None
        self.flush()
        self.emit("result", status=status, summary=str(summary), count=count, links=link_list, main=main_path)

    def item(self, key: str | int, status: str, msg: Any = None, row: int | None = None) -> None:
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise TypeError(f"item: key должен быть строкой или числом, получено {type(key).__name__}")
        if isinstance(key, str) and not key.strip():
            raise ValueError("item: key пустой")
        if status not in ITEM_STATUSES:
            raise ValueError(f"item: статус {status!r}, допустимы: {', '.join(ITEM_STATUSES)}")
        if row is not None and (isinstance(row, bool) or not isinstance(row, int)):
            raise TypeError(f"item: row должен быть целым (поле _row строки таблицы), получено {type(row).__name__}")
        text = None if msg is None else str(msg)
        if text is not None and len(text) > ITEM_MSG_MAX:
            text = text[: ITEM_MSG_MAX - 1] + "…"
        self.emit("item", key=key, status=status, msg=text or None, row=row)

    def session(self, action: str, session_id: str | None = None, browser_id: str | None = None) -> None:
        if action not in SESSION_ACTIONS:
            raise ValueError(f"действие {action!r}, допустимы: {', '.join(SESSION_ACTIONS)}")
        if action in ("open", "close") and not session_id:
            raise ValueError(f"session {action}: нужен session_id")
        self.emit("session", action=action, session_id=session_id, browser_id=browser_id)


class Log:
    """`kit.log.info(...)` и соседи."""

    def __init__(self, get_emitter: Callable[[], Emitter]):
        self._get = get_emitter

    def debug(self, msg: Any) -> None:
        self._get().log("debug", msg)

    def info(self, msg: Any) -> None:
        self._get().log("info", msg)

    def warn(self, msg: Any) -> None:
        self._get().log("warn", msg)

    warning = warn

    def error(self, msg: Any) -> None:
        self._get().log("error", msg)
