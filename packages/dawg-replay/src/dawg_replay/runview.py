"""Состояние запуска для экрана запуска (ТЗ 5.4, 6.4 п.3). Без веба.

    view = RunView.for_run(app, run_id)
    snap = view.snapshot()                 # первая отрисовка, в нём seq
    for batch in view.follow(after=snap["seq"]):
        send_sse(batch)                    # список изменений после seq

Источник: `events.jsonl` запуска, который читается инкрементально: файл
дочитывается с последней позиции, неполная последняя строка ждёт продолжения.
Итог из БД (статус, причина, сводка) и подозрительные сессии из `insurance.json`
подмешиваются отдельно. Запуски из CLI и из UI выглядят одинаково: всё
берётся из каталога запуска и БД.

Решения:

- `rows`: на таблицу хранятся первые `rows_limit` строк (настройка
  `rows_preview_limit`), остальные только считаются (`total`, `truncated`).
  Превью стабильно и совпадает с началом файла результатов, новые строки не
  выталкивают уже показанные.
- `columns`: из события, если скрипт их передал (первый непустой список), иначе
  ключи строк в порядке появления.
- лента лога хранит последние `log_limit` записей; строки страховки
  (`source = "dawg-replay"`) помечены `source`. Неизвестный `t` и битые строки
  попадают в ленту как `level = "raw"`, состояние от них не ломается.
- `artifacts`: путь проверяется: внутри каталога `output` и файл существует;
  путь наружу отклоняется записью в ленте.
- `item` (контракт 1.2): отчёт по строкам входа. Повторный `item` с тем же `key`
  заменяет прежний и остаётся на своём месте; ключи 1 и "1" это одна строка.
  Хранятся все строки (таблица на входе ограничена `max_rows`), счётчики по статусам.
- браузеры: `open` открывает, `close` закрывает, `failed` закрывает
  `provisioning`; `browsers.open` показывает, сколько открыто сейчас.

Изменения нумеруются (`seq`). `changes(after)` отдаёт всё после номера; если
клиент отстал дальше, чем хранится (`history`), он получает `resync` и должен
взять `snapshot()` заново.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE_APP = "dawg-replay"
LOG_LEVELS = ("debug", "info", "warn", "error")
RESULT_STATUSES = ("ok", "partial", "failed")
ITEM_STATUSES = ("ok", "failed", "skipped")
DEFAULT_ROWS_LIMIT = 500
DEFAULT_LOG_LIMIT = 2000
DEFAULT_HISTORY = 5000
READ_CHUNK = 1 << 20
HEAD_BYTES = 256  # по началу файла видно, что его подменили, даже если размер не уменьшился

RunInfo = Callable[[], "dict[str, Any] | None"]


@dataclass
class Change:
    seq: int
    kind: str  # stage, progress, metric, rows, item, log, artifact, browsers, result, run, reset
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "data": self.data}


class RunView:
    def __init__(
        self,
        run_dir: str | Path,
        *,
        rows_limit: int = DEFAULT_ROWS_LIMIT,
        log_limit: int = DEFAULT_LOG_LIMIT,
        history: int = DEFAULT_HISTORY,
        run_info: RunInfo | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.events_path = self.run_dir / "events.jsonl"
        self.output_dir = self.run_dir / "output"
        self.rows_limit = rows_limit
        self.log_limit = log_limit
        self._run_info = run_info
        self._lock = threading.RLock()
        self._history: deque[Change] = deque(maxlen=history)
        self._seq = 0
        self._reset_state()

    @classmethod
    def for_run(cls, app: Any, run_id: str, **kw: Any) -> RunView:
        """Вид запуска из каталога данных приложения: итог из БД, лимит строк из config."""
        from dawg_replay import diagnosis, insurance

        run_dir = app.paths.run_dir(run_id)
        app.runs.get(run_id)  # понятная ошибка для неизвестного id

        def run_info() -> dict[str, Any]:
            rec = app.runs.get(run_id)
            return {
                **{k: v for k, v in rec.to_dict().items() if k in _RUN_FIELDS},
                "suspects": insurance.pending_suspects(run_dir),
                # упал, ничего не сообщив: хвост stderr и подсказка (dawg_replay.diagnosis)
                "diagnosis": diagnosis.diagnose(app, rec),
            }

        kw.setdefault("rows_limit", app.config.rows_preview_limit)
        return cls(run_dir, run_info=run_info, **kw)

    # --- состояние ---------------------------------------------------------------

    def _reset_state(self) -> None:
        self._pos = 0
        self._head = b""
        self._buf = b""
        self._line_no = 0
        self.stage: str | None = None
        self.timeline: list[dict[str, Any]] = []
        self.progress: dict[str, dict[str, Any]] = {}
        self.metrics: dict[str, dict[str, Any]] = {}
        self.tables: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}  # str(key) -> строка отчёта, в порядке первого появления
        self.item_counts: dict[str, int] = dict.fromkeys(ITEM_STATUSES, 0)
        self.log: deque[dict[str, Any]] = deque(maxlen=self.log_limit)
        self.log_total = 0
        self.artifacts: list[dict[str, Any]] = []
        self.open_sessions: dict[str, dict[str, Any]] = {}
        self.provisioning = 0
        self.result: dict[str, Any] | None = None
        self.run: dict[str, Any] | None = None

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def position(self) -> int:
        """Сколько байт файла событий уже разобрано."""
        return self._pos

    def snapshot(self) -> dict[str, Any]:
        """Полное состояние для первой отрисовки. Сначала дочитывает файл."""
        with self._lock:
            self.poll()
            return {
                "seq": self._seq,
                "position": self._pos,
                "run": self.run,
                "stage": self.stage,
                "timeline": list(self.timeline),
                "progress": dict(self.progress),
                "metrics": dict(self.metrics),
                "tables": {name: self._table_view(t) for name, t in self.tables.items()},
                "items": self._items_view(),
                "log": list(self.log),
                "log_total": self.log_total,
                "artifacts": list(self.artifacts),
                "browsers": self._browsers(),
                "result": self.result,
            }

    # --- чтение файла ------------------------------------------------------------

    def poll(self) -> list[Change]:
        """Дочитывает новые строки и обновляет итог из БД. Возвращает изменения."""
        with self._lock:
            start = self._seq
            self._read_new()
            self._refresh_run()
            return [c for c in self._history if c.seq > start]

    def _read_new(self) -> None:
        try:
            size = self.events_path.stat().st_size
        except FileNotFoundError:
            return
        if size < self._pos or (self._head and not self._same_head()):
            # Файл подменили или обрезали: начинаем заново, клиентам нужен новый снимок.
            run = self.run
            self._reset_state()
            self.run = run
            self._emit("reset", {})
        if size == self._pos:
            return
        with open(self.events_path, "rb") as f:
            f.seek(self._pos)
            while True:
                chunk = f.read(READ_CHUNK)
                if not chunk:
                    break
                if len(self._head) < HEAD_BYTES:
                    self._head += chunk[: HEAD_BYTES - len(self._head)]
                self._pos += len(chunk)
                self._buf += chunk
                *lines, self._buf = self._buf.split(b"\n")
                for raw in lines:
                    self._line_no += 1
                    self._apply_line(raw)

    def _same_head(self) -> bool:
        try:
            with open(self.events_path, "rb") as f:
                return f.read(len(self._head)) == self._head
        except OSError:
            return False

    def _apply_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if not line.strip():
            return
        try:
            event = json.loads(line)
        except ValueError:
            self._raw(line, "битая строка")
            return
        if not isinstance(event, dict) or not isinstance(event.get("t"), str):
            self._raw(line, "строка без поля t")
            return
        handler = _HANDLERS.get(event["t"])
        if handler is None:
            self._raw(line, f"неизвестный тип события `{event['t']}`")
            return
        try:
            handler(self, event)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self._raw(line, f"событие `{event['t']}` не разобрано: {e}")

    # --- обработчики событий -----------------------------------------------------

    def _on_log(self, e: dict[str, Any]) -> None:
        level = e.get("level")
        entry = {"ts": e.get("ts"), "level": level if level in LOG_LEVELS else "info", "msg": str(e["msg"])}
        if e.get("source"):
            entry["source"] = str(e["source"])
        self._add_log(entry)

    def _on_stage(self, e: dict[str, Any]) -> None:
        name = str(e["name"])
        self.stage = name
        item = {"name": name, "ts": e.get("ts")}
        self.timeline.append(item)
        self._emit("stage", {"stage": name, "timeline_item": item})

    def _on_progress(self, e: dict[str, Any]) -> None:
        pid = str(e["id"])
        done = e["done"]
        if not isinstance(done, (int, float)) or isinstance(done, bool):
            raise ValueError("done не число")
        item = {"id": pid, "label": str(e.get("label", pid)), "done": done, "total": e.get("total"), "ts": e.get("ts")}
        self.progress[pid] = item
        self._emit("progress", item)

    def _on_metric(self, e: dict[str, Any]) -> None:
        key = str(e["key"])
        item = {"key": key, "label": str(e.get("label", key)), "value": e["value"], "unit": e.get("unit"), "ts": e.get("ts")}
        self.metrics[key] = item
        self._emit("metric", item)

    def _on_rows(self, e: dict[str, Any]) -> None:
        name = str(e["table"])
        rows = e["rows"]
        if not isinstance(rows, list):
            raise ValueError("rows не список")
        rows = [r for r in rows if isinstance(r, dict)]
        t = self.tables.setdefault(name, {"columns": None, "keys": [], "rows": [], "total": 0})
        columns = e.get("columns")
        if t["columns"] is None and isinstance(columns, list) and columns:
            t["columns"] = [str(c) for c in columns]
        added: list[dict[str, Any]] = []
        for r in rows:
            for k in r:
                if k not in t["keys"]:
                    t["keys"].append(k)
            if len(t["rows"]) < self.rows_limit:
                t["rows"].append(r)
                added.append(r)
        t["total"] += len(rows)
        view = self._table_view(t, rows=False)
        self._emit("rows", {"table": name, **view, "added": added})

    def _table_view(self, t: dict[str, Any], rows: bool = True) -> dict[str, Any]:
        out = {
            "columns": t["columns"] or list(t["keys"]),
            "total": t["total"],
            "shown": len(t["rows"]),
            "truncated": t["total"] > len(t["rows"]),
        }
        if rows:
            out["rows"] = list(t["rows"])
        return out

    def _on_item(self, e: dict[str, Any]) -> None:
        key = e["key"]
        if isinstance(key, bool) or not isinstance(key, (str, int)):
            raise ValueError("key не строка и не число")
        status = e["status"]
        if status not in ITEM_STATUSES:
            raise ValueError(f"статус {status!r}")
        row = e.get("row")
        item = {
            "key": key,
            "status": status,
            "msg": None if e.get("msg") is None else str(e["msg"]),
            "row": row if isinstance(row, int) and not isinstance(row, bool) else None,
            "ts": e.get("ts"),
        }
        old = self.items.get(str(key))
        if old is not None:
            self.item_counts[old["status"]] -= 1
        self.item_counts[status] += 1
        self.items[str(key)] = item
        self._emit("item", {"item": item, "counts": self._item_counts()})

    def _item_counts(self) -> dict[str, int]:
        return {**self.item_counts, "total": len(self.items)}

    def _items_view(self) -> dict[str, Any] | None:
        if not self.items:
            return None
        return {"counts": self._item_counts(), "rows": list(self.items.values())}

    def _on_artifact(self, e: dict[str, Any]) -> None:
        rel = str(e["path"])
        out = self.output_dir.resolve()
        full = (out / rel).resolve()
        try:
            inside = full.relative_to(out)
        except ValueError:
            self._add_log({"ts": e.get("ts"), "level": "warn", "msg": f"файл результата вне каталога output отклонён: {rel}"})
            return
        exists = full.is_file()
        item = {
            "path": inside.as_posix(),
            "label": str(e.get("label") or inside.name),
            "mime": e.get("mime"),
            "exists": exists,
            "size": full.stat().st_size if exists else None,
            "ts": e.get("ts"),
        }
        self.artifacts = [a for a in self.artifacts if a["path"] != item["path"]] + [item]
        self._emit("artifact", item)

    def _on_session(self, e: dict[str, Any]) -> None:
        action = e["action"]
        sid = e.get("session_id")
        if action == "provisioning":
            self.provisioning += 1
        elif action == "open" and sid:
            self.provisioning = max(0, self.provisioning - 1)
            self.open_sessions[str(sid)] = {"session_id": str(sid), "browser_id": e.get("browser_id"), "ts": e.get("ts")}
        elif action == "failed":
            self.provisioning = max(0, self.provisioning - 1)
        elif action == "close" and sid:
            self.open_sessions.pop(str(sid), None)
        else:
            raise ValueError(f"действие {action!r}")
        self._emit("browsers", self._browsers())

    def _browsers(self) -> dict[str, Any]:
        return {
            "open": len(self.open_sessions),
            "provisioning": self.provisioning,
            "sessions": list(self.open_sessions.values()),
        }

    def _on_result(self, e: dict[str, Any]) -> None:
        status = e["status"]
        if status not in RESULT_STATUSES:
            raise ValueError(f"статус {status!r}")
        self.result = {"status": status, "summary": str(e.get("summary", "")), "ts": e.get("ts")}
        self._emit("result", self.result)

    # --- лента и изменения -------------------------------------------------------

    def _raw(self, line: str, why: str) -> None:
        self._add_log({"level": "raw", "msg": line, "note": why, "line": self._line_no})

    def _add_log(self, entry: dict[str, Any]) -> None:
        self.log.append(entry)
        self.log_total += 1
        self._emit("log", entry)

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        self._seq += 1
        self._history.append(Change(self._seq, kind, data))

    def _refresh_run(self) -> None:
        if self._run_info is None:
            return
        try:
            info = self._run_info()
        except Exception:
            return
        if info is not None and info != self.run:
            self.run = info
            self._emit("run", info)

    # --- поток изменений для SSE -------------------------------------------------

    def changes(self, after: int) -> dict[str, Any]:
        """Изменения с номером больше `after`. `resync`: клиент отстал, нужен snapshot()."""
        with self._lock:
            self.poll()
            oldest = self._history[0].seq if self._history else self._seq + 1
            if after < oldest - 1 or any(c.kind == "reset" and c.seq > after for c in self._history):
                return {"seq": self._seq, "resync": True, "changes": []}
            return {"seq": self._seq, "resync": False, "changes": [c.to_dict() for c in self._history if c.seq > after]}

    @property
    def finished(self) -> bool:
        """Запуск завершён (по БД). Без БД: есть событие result."""
        if self._run_info is not None:
            return bool(self.run) and self.run.get("status") not in ("running", "waiting")
        return self.result is not None

    def follow(
        self,
        after: int = 0,
        interval: float = 0.25,
        stop: threading.Event | None = None,
        idle_after_finish: float = 1.0,
    ) -> Iterator[dict[str, Any]]:
        """Генератор пачек изменений (как `changes`). Заканчивается, когда запуск завершён и
        файл событий дочитан (с запасом `idle_after_finish` на строки страховки), или по `stop`."""
        stop = stop or threading.Event()
        seq = after
        finished_at: float | None = None
        while not stop.is_set():
            batch = self.changes(seq)
            if batch["resync"] or batch["changes"]:
                yield batch
                seq = batch["seq"]
                if batch["resync"]:
                    return
            if self.finished:
                if finished_at is None:
                    finished_at = time.monotonic()
                elif time.monotonic() - finished_at >= idle_after_finish and not batch["changes"]:
                    return
            stop.wait(interval)


_RUN_FIELDS = (
    "id", "script", "version", "draft", "status", "reason", "label", "created_at",
    "started_at", "finished_at", "exit_code", "summary", "error", "killed", "cancel_requested",
    "trigger", "schedule_id",
)

_HANDLERS: dict[str, Callable[[RunView, dict[str, Any]], None]] = {
    "log": RunView._on_log,
    "stage": RunView._on_stage,
    "progress": RunView._on_progress,
    "metric": RunView._on_metric,
    "rows": RunView._on_rows,
    "item": RunView._on_item,
    "artifact": RunView._on_artifact,
    "session": RunView._on_session,
    "result": RunView._on_result,
}
