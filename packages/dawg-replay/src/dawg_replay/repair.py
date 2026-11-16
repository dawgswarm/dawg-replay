"""Починка с агентом и тихая поломка (сценарий C в один клик).

    s = suspicion(app, rec)        # None или {"kind": "empty"|"drop", "text": ..., ...}
    p = fix_prompt(app, rec)       # {"text": промпт для Claude Code или Codex, ...}

Тихая поломка: запуск закончился `ok` или `partial`, но похоже, что сайт
поменялся. Счётчик собранного берётся из событий, которые уже есть в контракте:

- отчёт по строкам `item` (контракт 1.2): сколько строк сделано, то есть `ok` и
  `skipped` (повторный запуск пропускает уже сделанное, это не поломка); повторный
  `item` той же строки (`row`, без него `key`) заменяет прежний;
- иначе сумма строк всех таблиц из `rows` (`total`, не только превью);
- если и `rows` не было, `done` первой полоски `progress`.

`metric` не используется: у метрик нет общего смысла (цена, процент, «найдено на
сайте»), по ключу не понять, что из них «собрано».

Правила:

1. `empty`: счётчик есть и равен 0, и нет ни одного файла результата. Если скрипт
   не присылал ни `rows`, ни `progress`, судить не по чему: такой запуск не
   помечается.
0. `missing_output`: скрипт объявил файлы результата (контракт 1.4, `[[outputs]]`), а
   после запуска какого-то нет. Сверку делает `outputs.check_run` и пишет строку в
   журнал запуска с полем `output`, отсюда она и читается.
2. `drop`: есть хотя бы MIN_HISTORY прошлых запусков `ok` того же скрипта и
   пресета (без пресета: с теми же параметрами) с тем же источником счётчика,
   медиана их счётчиков не меньше MIN_BASELINE, а текущий меньше
   DROP_RATIO * медианы. Медиана последних HISTORY запусков не боится одного
   выброса; половина — заметно больше обычного разброса выдачи между запусками,
   а сломанный селектор обычно даёт ноль или кусок страницы.

Промпт собирается целиком и потом проходит маскировку (`diagnosis.scrub`):
ключ DAWG, сохранённые секреты скрипта, токены в адресах и заголовках.
Параметры запуска в БД уже лежат без секретов.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dawg_replay import asks, diagnosis
from dawg_replay.errors import ReplayError
from dawg_replay.runview import item_id
from dawg_replay.values import brief

MIN_HISTORY = 3
HISTORY = 10
MIN_BASELINE = 10
DROP_RATIO = 0.5
HISTORY_SCAN = 200  # сколько последних запусков скрипта смотреть в поисках истории

PROMPT_TAIL_LINES = 30
PROMPT_ERRORS = 5
ERROR_CHARS = 500

SKILL = "dawg-replay-builder"
NOT_SCRIPT_FAULT = ("cancelled", "skipped", "not_started")

REASONS = {
    "cancelled": "запуск отменили",
    "timeout": "запуск шёл дольше разрешённого и был остановлен",
    "lost": "запуск прервался: программа, которая его вела, закрылась или компьютер выключился",
    "start_error": "скрипт не удалось запустить",
}


# --- разбор events.jsonl ---------------------------------------------------------


@dataclass
class Scan:
    rows: dict[str, int] = field(default_factory=dict)  # таблица -> всего строк
    progress: dict[str, float] = field(default_factory=dict)  # id -> последний done, в порядке появления
    files: list[str] = field(default_factory=list)  # файлы результата, которые есть на диске
    errors: list[str] = field(default_factory=list)  # последние сообщения уровня error
    items: dict[str, int] = field(default_factory=dict)  # статус item -> сколько строк, пусто без item
    count: int | None = None  # result.count (контракт 1.3): скрипт сам сказал, сколько собрал
    missing: list[str] = field(default_factory=list)  # объявленные выходы, которых нет (outputs.check_run)

    def counter(self) -> tuple[str, int] | None:
        """(источник, значение) или None, если скрипт ничего не считал. Явный count первым."""
        if self.count is not None:
            return "count", self.count
        if self.items:
            return "items", self.items.get("ok", 0) + self.items.get("skipped", 0)
        if self.rows:
            return "rows", sum(self.rows.values())
        for pid, done in self.progress.items():
            return f"progress:{pid}", int(done)
        return None


CACHE_FILE = "repair.json"
CACHE_VERSION = 5
ITEM_STATUSES = ("ok", "failed", "skipped")
_cache: dict[str, tuple[int, int, _Raw]] = {}
_cache_lock = threading.Lock()
CACHE_SIZE = 1024


def scan(run_dir: Path) -> Scan:
    """Разбор событий запуска. Кешируется по размеру и времени изменения файла: в памяти
    процесса и в `repair.json` в каталоге запуска (как `minutes.json`). Списки запусков
    (библиотека, история, главная) не перечитывают большие events.jsonl при каждом показе.
    Наличие файлов результата проверяется при каждом вызове: их могли удалить."""
    path = run_dir / "events.jsonl"
    try:
        st = path.stat()
    except OSError:
        return _with_files(_parse(path), run_dir)
    stamp = [st.st_size, st.st_mtime_ns]
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
    if hit and [hit[0], hit[1]] == stamp:
        return _with_files(hit[2], run_dir)
    raw = _load_cache(run_dir, stamp)
    if raw is None:
        raw = _parse(path)
        _save_cache(run_dir, stamp, raw)
    with _cache_lock:
        if len(_cache) >= CACHE_SIZE:
            _cache.clear()
        _cache[key] = (stamp[0], stamp[1], raw)
    return _with_files(raw, run_dir)


def _load_cache(run_dir: Path, stamp: list[int]) -> _Raw | None:
    try:
        data = json.loads((run_dir / CACHE_FILE).read_text(encoding="utf-8"))
        if data.get("v") != CACHE_VERSION or data.get("stamp") != stamp:
            return None
        return _Raw(
            rows={str(k): int(v) for k, v in data["rows"].items()},
            progress={str(k): float(v) for k, v in data["progress"].items()},
            artifacts=[str(x) for x in data["artifacts"]],
            errors=[str(x) for x in data["errors"]],
            items={str(k): int(v) for k, v in data["items"].items()},
            count=None if data.get("count") is None else int(data["count"]),
            missing=[str(x) for x in data["missing"]],
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _save_cache(run_dir: Path, stamp: list[int], raw: _Raw) -> None:
    if not run_dir.is_dir():
        return
    cache = run_dir / CACHE_FILE
    tmp = cache.with_name(f"{CACHE_FILE}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps({"v": CACHE_VERSION, "stamp": stamp, "rows": raw.rows, "progress": raw.progress,
                                   "artifacts": raw.artifacts, "errors": raw.errors, "items": raw.items,
                                   "count": raw.count, "missing": raw.missing},
                                  ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


@dataclass
class _Raw:
    """То, что зависит только от файла событий."""

    rows: dict[str, int] = field(default_factory=dict)
    progress: dict[str, float] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)  # пути из событий artifact, без повторов
    errors: list[str] = field(default_factory=list)
    items: dict[str, int] = field(default_factory=dict)
    count: int | None = None
    missing: list[str] = field(default_factory=list)


def _with_files(raw: _Raw, run_dir: Path) -> Scan:
    s = Scan(rows=dict(raw.rows), progress=dict(raw.progress), errors=list(raw.errors), items=dict(raw.items),
             count=raw.count, missing=list(raw.missing))
    out = (run_dir / "output").resolve()
    for rel in raw.artifacts:
        try:
            full = (out / rel).resolve()
            full.relative_to(out)
        except (OSError, ValueError, RuntimeError):
            continue
        if full.is_file():
            s.files.append(full.relative_to(out).as_posix())
    return s


def _parse(path: Path) -> _Raw:
    s = _Raw()
    try:
        f = open(path, "rb")
    except OSError:
        return s
    artifacts: list[str] = []
    items: dict[str, str] = {}
    with f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            t = e.get("t")
            try:
                if t == "rows" and isinstance(e.get("rows"), list):
                    name = str(e.get("table"))
                    s.rows[name] = s.rows.get(name, 0) + sum(1 for r in e["rows"] if isinstance(r, dict))
                elif t == "progress":
                    done = e.get("done")
                    if isinstance(done, (int, float)) and not isinstance(done, bool):
                        s.progress[str(e["id"])] = done
                elif t == "item" and e.get("status") in ITEM_STATUSES and item_id(e.get("key"), e.get("row")):
                    items[item_id(e["key"], e.get("row"))] = e["status"]
                elif t == "result":
                    c = e.get("count")  # последний итог решает, без count сбрасывает
                    s.count = c if isinstance(c, int) and not isinstance(c, bool) and c >= 0 else None
                elif t == "artifact" and e.get("path"):
                    artifacts.append(str(e["path"]))
                elif t == "log" and e.get("source") == MISSING_SOURCE and isinstance(e.get("output"), str):
                    if e["output"] not in s.missing:
                        s.missing.append(e["output"])
                elif t == "log" and e.get("level") == "error" and e.get("msg") is not None:
                    s.errors.append(str(e["msg"])[:ERROR_CHARS])
                    del s.errors[:-PROMPT_ERRORS]
            except (KeyError, TypeError):
                continue
    s.artifacts = list(dict.fromkeys(artifacts))
    for status in items.values():
        s.items[status] = s.items.get(status, 0) + 1
    return s


def items_counts(raw: dict[str, int]) -> dict[str, int] | None:
    """Счётчики отчёта по строкам для списков: ok, failed, skipped, total. None без item."""
    if not raw:
        return None
    out = {k: raw.get(k, 0) for k in ITEM_STATUSES}
    out["total"] = sum(out.values())
    return out


# --- тихая поломка ---------------------------------------------------------------


def _same_group(a: Any, b: Any) -> bool:
    if a.preset or b.preset:
        return a.preset == b.preset
    return a.params == b.params


def baseline(app: Any, rec: Any, source: str, history: list[Any] | None = None) -> list[int]:
    """Счётчики прошлых успешных запусков того же скрипта и пресета, новые первыми.
    `history`: уже прочитанные запуски скрипта (новые первыми), чтобы список не читал БД на каждую строку."""
    out: list[int] = []
    recs = history if history is not None else app.runs.list(rec.script_name, HISTORY_SCAN)
    for r in recs:
        if len(out) >= HISTORY:
            break
        if r.id == rec.id or r.draft or r.status != "ok" or (r.created_at, r.id) >= (rec.created_at, rec.id):
            continue
        if not _same_group(r, rec):
            continue
        c = scan(app.paths.run_dir(r.id)).counter()
        if c and c[0] == source:
            out.append(c[1])
    return out


def judge(counter: tuple[str, int] | None, has_files: bool, history: list[int],
          missing: list[str] | None = None) -> dict[str, Any] | None:
    """Правила тихой поломки без доступа к приложению (см. описание модуля)."""
    if missing:
        names = ", ".join(missing[:5]) + (f" и ещё {len(missing) - 5}" if len(missing) > 5 else "")
        return {
            "kind": "missing_output",
            "value": None,
            "median": None,
            "missing": list(missing),
            "text": f"Запуск завершился без ошибки, но {'нет файла' if len(missing) == 1 else 'нет файлов'}, "
                    f"которые скрипт обещал отдать: {names}.",
        }
    if counter is None:
        return None  # скрипт ничего не считал: судить не по чему
    value = counter[1]
    what = _what(counter[0], value)
    if value == 0 and not has_files and counter[0] == "items":
        return {
            "kind": "empty",
            "value": 0,
            "median": None,
            "text": "Запуск завершился без ошибки, но ни одна строка не сделана.",
        }
    if value == 0 and counter[0] == "count":
        # Скрипт сам сказал «0 записей»: файл с одной шапкой этого не оправдывает.
        return {
            "kind": "empty",
            "value": 0,
            "median": None,
            "text": "Запуск завершился без ошибки, но скрипт сообщил, что собрал 0 записей.",
        }
    if value == 0 and not has_files:
        return {
            "kind": "empty",
            "value": 0,
            "median": None,
            "text": "Запуск завершился без ошибки, но ничего не собрал и не сохранил ни одного файла.",
        }
    if len(history) < MIN_HISTORY:
        return None
    med = statistics.median(history)
    if med < MIN_BASELINE or value >= med * DROP_RATIO:
        return None
    med_text = _num(round(med))
    return {
        "kind": "drop",
        "value": value,
        "median": med,
        "runs": len(history),
        "text": (
            f"{'Сделано' if counter[0] == 'items' else 'Собрано'} {_num(value)} {what}, а в последних {len(history)} успешных запусках с теми же "
            f"настройками обычно около {med_text}."
        ),
    }


def suspicion(app: Any, rec: Any, history: list[Any] | None = None) -> dict[str, Any] | None:
    """Похоже, сайт поменялся: None или {"kind", "text", "value", "median"}."""
    if rec.running or rec.draft or rec.status not in ("ok", "partial"):
        return None
    s = scan(app.paths.run_dir(rec.id))
    counter = s.counter()
    hist = baseline(app, rec, counter[0], history) if counter else []
    return judge(counter, bool(s.files), hist, s.missing)


def annotate(app: Any, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Добавляет `suspicion` в словари запусков (Run.to_dict) для списков. История каждого
    скрипта читается из БД один раз на вызов, разбор событий берётся из кэша."""
    by_script: dict[str, list[Any]] = {}
    annotate_items(app, runs)
    for d in runs:
        d["suspicion"] = None
        if d.get("status") not in ("ok", "partial") or d.get("draft"):
            continue
        name = d["script"]
        if name not in by_script:
            by_script[name] = app.runs.list(name, HISTORY_SCAN)
        recs = by_script[name]
        rec = next((r for r in recs if r.id == d["id"]), None)
        if rec is None:
            try:
                rec = app.runs.get(d["id"])
            except ReplayError:
                continue
        try:
            d["suspicion"] = suspicion(app, rec, recs)
        except OSError:
            d["suspicion"] = None
    return runs


MISSING_SOURCE = "dawg-replay-outputs"  # source строки журнала о недостающем выходе


TABLES_FILE = "tables.json"


RELATIVE_FILE = "relative.json"


def write_relative(run_dir: Path, exprs: dict[str, str]) -> None:
    """`relative.json`: из какого выражения взята дата запуска (`{"since": "today-1"}`)."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / RELATIVE_FILE).write_text(json.dumps(exprs, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_relative(run_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((run_dir / RELATIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def write_table_sizes(run_dir: Path, sizes: dict[str, int]) -> None:
    """`tables.json` в каталоге запуска: число строк каждой таблицы-параметра. Только числа,
    поэтому годится и для секретной таблицы, которой нет в истории."""
    if not sizes:
        return
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / TABLES_FILE).write_text(json.dumps(sizes), encoding="utf-8")
    except OSError:
        pass


def read_table_sizes(run_dir: Path) -> dict[str, int]:
    try:
        sizes = json.loads((run_dir / TABLES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in sizes.items() if isinstance(v, int)} if isinstance(sizes, dict) else {}


def table_size(run_dir: Path, params: dict[str, Any] | None = None) -> int | None:
    """Число строк единственной таблицы запуска: из `tables.json`, у старых запусков из параметров."""
    try:
        sizes = json.loads((run_dir / TABLES_FILE).read_text(encoding="utf-8"))
        if isinstance(sizes, dict) and len(sizes) == 1:
            (n,) = sizes.values()
            if isinstance(n, int) and not isinstance(n, bool):
                return n
        if isinstance(sizes, dict):
            return None
    except (OSError, ValueError):
        pass
    tables = [v for v in (params or {}).values()
              if isinstance(v, list) and v and all(isinstance(r, dict) and "_row" in r for r in v)]
    return len(tables[0]) if len(tables) == 1 else None


def annotate_items(app: Any, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Добавляет `items` (счётчики отчёта по строкам или None) в словари запусков."""
    for d in runs:
        try:
            d["items"] = items_counts(scan(app.paths.run_dir(d["id"])).items)
        except (OSError, ValueError):
            d["items"] = None
        if d["items"]:
            # Запуск оборвался посреди таблицы: «30 из 50», а не «30 из 30».
            n = table_size(app.paths.run_dir(d["id"]), d.get("params"))
            if n is not None and n > d["items"]["total"]:
                d["items"]["total"] = n
    return runs


def needs_repair(app: Any, rec: Any) -> dict[str, Any] | None:
    """Показывать ли «Починить с агентом»: упал не по отмене или тихая поломка.
    Возвращает {"suspicion": ...} или None."""
    if rec.running:
        return None
    if rec.status == "failed":
        # Отменили сами или не дождался слота (dr-52): скрипт не виноват, чинить нечего.
        if rec.reason in NOT_SCRIPT_FAULT:
            return None
        # Не ответили на вопрос скрипта (kit.AskTimeout): тоже не поломка.
        if rec.exit_code not in (0, None) and asks.timed_out(app.paths.run_dir(rec.id)) is not None:
            return None
        return {"suspicion": None}
    s = suspicion(app, rec)
    return {"suspicion": s} if s else None


def _what(source: str, n: float | None = None) -> str:
    if source not in ("rows", "items", "count"):
        return f"по полоске «{source.split(':', 1)[1]}»"
    forms = ("запись", "записи", "записей") if source == "count" else ("строка", "строки", "строк")
    if n is None or not float(n).is_integer():
        return forms[2]
    n = int(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _num(v: float) -> str:
    if float(v).is_integer():
        return f"{int(v):,}".replace(",", " ")
    return f"{v:.1f}"


# --- промпт ----------------------------------------------------------------------


def _title(app: Any, rec: Any) -> tuple[str, int | None]:
    """Название версии запуска и номер текущей версии в приложении."""
    title, current = rec.script_name, None
    try:
        current = app.registry.get(rec.script_name).current_version
        title = app.registry.version(rec.script_name, rec.script_version).title
    except ReplayError:
        pass
    return title, current


def _params(params: dict[str, Any]) -> list[str]:
    return [f"  {k} = {json.dumps(v, ensure_ascii=False)}" for k, v in brief(params).items()]


def _what_happened(rec: Any, sus: dict[str, Any] | None) -> list[str]:
    lines: list[str] = []
    if sus:
        lines.append(sus["text"])
    if rec.status == "failed":
        if rec.reason in REASONS:
            lines.append(REASONS[rec.reason].capitalize() + ".")
        elif rec.exit_code not in (None, 0):
            lines.append(f"Скрипт завершился с кодом {rec.exit_code}.")
    if rec.summary:
        lines.append(f"Итог от скрипта: {rec.summary}")
    if rec.error:
        lines.append(f"Ошибка приложения: {rec.error}")
    return lines


def fix_prompt(app: Any, rec: Any) -> dict[str, Any]:
    """Готовый текст для агента и его составные части (для --json)."""
    if rec.running:
        raise ReplayError("run_running", f"запуск `{rec.id}` ещё идёт; промпт починки будет после его завершения")
    run_dir = app.paths.run_dir(rec.id)
    s = scan(run_dir)
    sus = suspicion(app, rec)
    title, current = _title(app, rec)
    secrets = diagnosis.known_secrets(app, rec.script_name)
    tail = diagnosis.read_tail(run_dir / "stderr.log", PROMPT_TAIL_LINES)
    hint = diagnosis.hint_for("\n".join(tail)) if tail else None
    name = rec.script_name

    if rec.status == "failed":
        headline = f"Скрипт dawg-replay сломался: запуск {rec.id} упал"
    elif sus:
        headline = f"Скрипт dawg-replay сломался: запуск {rec.id} прошёл без ошибки, но, похоже, сайт поменялся"
    else:
        headline = f"Скрипт dawg-replay собрал не то: запуск {rec.id}"
    out = [f"{headline}. Почини его по скиллу {SKILL} (раздел «C. Скрипт сломался»).", ""]

    version = f", версия {rec.script_version}" if rec.script_version is not None else ""
    if current is not None and rec.script_version is not None and current != rec.script_version:
        version += f" (текущая в приложении: {current})"
    draft = ", черновой запуск файла без регистрации" if rec.draft else ""
    out.append(f"Скрипт: «{title}» ({name}){version}{draft}")
    out.append(f"Файл этой версии: {rec.script_path}")
    preset = f", пресет «{rec.preset}»" if rec.preset else ""
    out.append(f"Запуск: {rec.id}, итог {rec.label}, начат {rec.created_at}{preset}")
    if rec.params:
        out.append("Параметры запуска (секреты не показаны):")
        out += _params(rec.params)
    else:
        out.append("Параметры запуска: нет (секреты не показываются)")

    happened = _what_happened(rec, sus)
    if happened:
        out += ["", "Что случилось:"] + [f"- {x}" for x in happened]
    counter = s.counter()
    if counter is not None:
        out.append(f"- Собрано {_what(counter[0])}: {_num(counter[1])}; файлов результата: {len(s.files)}.")
    if hint:
        out += ["", f"Подсказка приложения: {hint}"]
    if s.errors:
        out += ["", "Последние ошибки из журнала скрипта:"] + [f"- {e}" for e in s.errors]
    if tail:
        out += ["", "Последние строки stderr:", "```"] + tail + ["```"]

    out += [
        "",
        "Подробности:",
        f"- dawg-replay logs {rec.id} --events  (события запуска)",
        f"- dawg-replay logs {rec.id}  (stdout и stderr)",
        f"- dawg-replay runs {name} --json  (история запусков)",
        "",
        "Как чинить: файл в хранилище приложения не правь. Возьми текущую версию "
        f"(dawg-replay show {name} --json, поле path) и работай с копией ./{name}/script.py; если там уже лежит "
        "черновик, сначала сравни его с текущей версией и не перезаписывай молча. Найди причину, при необходимости заново пройди проблемное место в браузере DAWG через dawg-mcp "
        "и освободи браузер. Подними version на 1, проверь прогоном на маленьких параметрах "
        f"и зарегистрируй: dawg-replay add ./{name}/script.py. Если причина не в коде "
        "(кончились минуты, неверный ключ, сайт не пропустил проверку, лимит браузеров), скрипт не правь, а скажи мне как есть.",
    ]
    text = diagnosis.scrub("\n".join(out), secrets)
    return {
        "text": text,
        "run_id": rec.id,
        "script": name,
        "version": rec.script_version,
        "script_path": rec.script_path,
        "suspicion": sus,
        "hint": hint,
    }

