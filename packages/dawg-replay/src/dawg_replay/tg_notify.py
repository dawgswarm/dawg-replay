"""Сообщение в Telegram об итоге запуска.

Кто отправляет:
- `launch.supervise` после `store.finish` и страховки браузеров, в отдельном потоке:
  цикл запуска и отмена Telegram не ждут, процесс-супервизор и `run --follow` дожидаются
  отправки перед выходом (поток не daemon, таймауты короткие);
- потерянные (`recover_lost`), не стартовавшие (`not_started`) и пропущенные (`skipped`)
  запуски отправляет закрывший их процесс, сразу и одной попыткой: это может быть
  короткая команда CLI.

Без дублей: перед отправкой запуск захватывается в БД (`notify_state` = sending),
захватить может только один процесс. Итог: sent, none (слать нечего) или failed.
Неотправленные (failed, или зависший sending, или никем не захваченный итог) пробует
ещё раз `retry_pending` из следующего процесса, который закрывает запуски
(`recover_lost`: команды CLI, старт UI). Запуски старше суток не отправляются.

Ошибка Telegram запуск не меняет: строка в журнале запуска и «последняя ошибка» в Настройках.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay import diagnosis, repair, tg_rules
from dawg_replay.db import connect
from dawg_replay.errors import ReplayError
from dawg_replay.runner import last_result
from dawg_replay.runs import REASON_LOST, REASON_NOT_STARTED, REASON_SKIPPED, REASON_START_ERROR, Run
from dawg_replay.runview import result_extras

if TYPE_CHECKING:
    from dawg_replay.app import App

SENDING, SENT, FAILED, NONE = "sending", "sent", "failed", "none"
MAX_AGE = timedelta(days=1)  # старше не отправляются
UNCLAIMED_AFTER = timedelta(minutes=2)  # итог без захвата: процесс, который должен был слать, пропал
STALE_SENDING = timedelta(minutes=10)  # захват без итога: отправлявший процесс умер
RETRY_LIMIT = 5  # запусков за один повтор
BACKGROUND_ATTEMPTS = 3
RETRY_PAUSE = 2.0
MAX_DOCUMENT = 50 * 1024 * 1024  # предел Bot API для sendDocument
LOG_SOURCE = "dawg-replay"


# --- обращения к ядру Telegram (dawg_replay.telegram), тесты подменяют ---------------


def _connection(app: App) -> Any:
    try:
        from dawg_replay import telegram
    except ImportError:  # TODO(dr-97): убрать после мержа ядра
        return None
    return telegram.connection(app)


def _bot(conn: Any) -> Any:
    from dawg_replay import telegram

    return telegram.Bot(conn.token, conn.api_url)


def _record_error(app: App, text: str) -> None:
    from dawg_replay import telegram

    telegram.record_error(app, text)


def _error_text(e: Exception, conn: Any) -> str:
    from dawg_replay import telegram

    text = getattr(e, "description", None) or str(e) or type(e).__name__
    return telegram.mask_token(text, getattr(conn, "token", None))


# --- время ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def duration_text(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def moment_text(dt: datetime | None) -> str:
    if dt is None:
        return ""
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    if local.date() == today:
        return f"сегодня {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"вчера {local:%H:%M}"
    return f"{local:%d.%m.%Y %H:%M}"


# --- текст ---------------------------------------------------------------------------


def _title(app: App, rec: Run) -> str:
    try:
        title = app.registry.get(rec.script_name).title or rec.script_name
    except ReplayError:
        title = rec.script_name
    return f"{title}, пресет «{rec.preset}»" if rec.preset else title


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _plain(text: str) -> str:
    """Итог скрипта бывает в Markdown; в простом тексте убираем только звёздочки и обратные кавычки."""
    return text.replace("**", "").replace("`", "")


def _rows_text(counts: dict[str, int] | None) -> str | None:
    """«48 из 50, 2 упали» по отчёту по строкам (kit.item)."""
    if not counts or not counts.get("total"):
        return None
    done = counts["ok"]
    parts = [f"{done} из {counts['total']}"]
    if counts["failed"]:
        parts.append(f"{counts['failed']} {_plural(counts['failed'], 'упала', 'упали', 'упали')}")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} {_plural(counts['skipped'], 'пропущена', 'пропущены', 'пропущено')}")
    return "Строки: " + ", ".join(parts)


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


FAILURE_TEXT = {
    "timeout": "Шёл дольше разрешённого и был остановлен.",
    REASON_LOST: "Прервался: программа, которая его вела, закрылась или компьютер выключился.",
    REASON_SKIPPED: "Не начался: все места для одновременных запусков были заняты, а время ожидания вышло.",
}


def failure_reason(app: App, rec: Run, scan: repair.Scan | None) -> str:
    """Причина падения одной строкой, как на экране запуска."""
    if rec.reason in FAILURE_TEXT:
        return FAILURE_TEXT[rec.reason]
    if rec.reason == REASON_NOT_STARTED:
        why = _first_line(rec.error)
        return "Плановый запуск не начался" + (f": {why}" if why else ".")
    if rec.reason == REASON_START_ERROR:
        why = _first_line(rec.error)
        return "Скрипт не удалось запустить" + (f": {why}" if why else ".")
    try:
        dg = diagnosis.diagnose(app, rec)
    except OSError:
        dg = None
    if dg and dg.get("ask_timeout"):
        return dg["summary"]
    if rec.summary:
        return _first_line(_plain(rec.summary))
    if scan and scan.errors:
        return _first_line(scan.errors[-1])
    if dg and dg.get("summary"):
        return dg["summary"]
    if rec.exit_code not in (0, None):
        return f"Скрипт завершился с ошибкой (код {rec.exit_code})."
    return _first_line(rec.error) or "Запуск не удался."


@dataclass
class Message:
    text: str
    document: Path | None = None


def compose(app: App, rec: Run, plan: tg_rules.Plan) -> Message:
    """Текст сообщения об итоге. Значения секретов и всё похожее на секрет скрыто (diagnosis.scrub)."""
    run_dir = app.paths.run_dir(rec.id)
    try:
        scan = repair.scan(run_dir) if run_dir.is_dir() else None
    except OSError:
        scan = None
    title = _title(app, rec)
    lines: list[str] = []
    document = None
    if plan.kind == "failed":
        lines.append(f"Упал: {title}")
        lines.append(failure_reason(app, rec, scan))
    else:
        lines.append(f"{'Готово частично' if rec.status == 'partial' else 'Готово'}: {title}")
        if rec.summary:
            lines.append(_plain(rec.summary.strip()))
        rows = _rows_text(repair.items_counts(scan.items) if scan else None)
        if rows:
            lines.append(rows)
        if scan and scan.count is not None:
            lines.append(f"Собрано записей: {scan.count}")
        if plan.suspicion:
            lines.append(f"Похоже, сайт поменялся. {plan.suspicion['text']}")
        result = last_result(run_dir / "events.jsonl") if run_dir.is_dir() else None
        extras = result_extras(result, run_dir / "output") if result else {"links": [], "main": None}
        for link in extras["links"]:
            lines.append(f"{link['title']}: {link['url']}" if link["title"] != link["url"] else link["url"])
        if plan.attach_main and extras["main"]:
            path = run_dir / "output" / extras["main"]
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            if size is not None and size <= MAX_DOCUMENT:
                document = path
            elif size is not None:
                lines.append(f"Файл {path.name} больше 50 МБ, он есть в приложении.")
    took = tg_rules.duration_seconds(rec)
    when = moment_text(tg_rules.started_local(rec))
    if took is not None and rec.started_at:
        lines.append(f"Шёл {duration_text(took)}, начат {when}")
    elif when:
        lines.append(f"Время: {when}")
    lines.append(f"В приложении: Запуски, {title}, {when}")
    text = diagnosis.scrub("\n".join(lines), diagnosis.known_secrets(app, rec.script_name))
    return Message(text, document)


# --- захват и отметки в БД -------------------------------------------------------------


def _claim(db: Any, run_id: str) -> bool:
    now = _now()
    cur = db.execute(
        "UPDATE runs SET notify_state = ?, notify_at = ? WHERE id = ? AND status IN ('ok', 'partial', 'failed')"
        " AND (notify_state IS NULL OR notify_state = ? OR (notify_state = ? AND notify_at < ?))",
        (SENDING, _iso(now), run_id, FAILED, SENDING, _iso(now - STALE_SENDING)),
    )
    return cur.rowcount == 1


def _mark(db: Any, run_id: str, state: str) -> None:
    db.execute("UPDATE runs SET notify_state = ?, notify_at = ? WHERE id = ?", (state, _iso(_now()), run_id))


def _log(run_dir: Path, level: str, msg: str) -> None:
    """Строка в журнал запуска (events.jsonl), как пишет страховка браузеров."""
    if not run_dir.is_dir():
        return
    e = {"t": "log", "ts": _iso(_now()), "level": level, "msg": msg, "source": LOG_SOURCE}
    try:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --- отправка ----------------------------------------------------------------------------


@dataclass
class Outgoing:
    run_id: str
    run_dir: Path
    conn: Any
    message: Message
    db_path: Path
    errors: list[str] = field(default_factory=list)


def _prepare(app: App, rec: Run) -> Outgoing | None:
    """Что отправить, или None, если слать нечего. Только локальная работа: БД, файлы, хранилище паролей."""
    finished = rec.finished_at and datetime.fromisoformat(rec.finished_at.replace("Z", "+00:00"))
    if finished and _now() - finished > MAX_AGE:
        return None
    if rec.draft:
        return None
    conn = _connection(app)
    if conn is None:
        return None
    sus = None
    if rec.status in ("ok", "partial"):
        try:
            sus = repair.suspicion(app, rec)
        except OSError:
            sus = None
    plan = tg_rules.plan(app, rec, sus)
    if not plan.send:
        return None
    return Outgoing(rec.id, app.paths.run_dir(rec.id), conn, compose(app, rec, plan), app.paths.db)


def _deliver(app: App, out: Outgoing, attempts: int) -> bool:
    bot = _bot(out.conn)
    chat = out.conn.chat_id
    for i in range(attempts):
        try:
            bot.send_message(chat, out.message.text)
            break
        except Exception as e:  # noqa: BLE001 отправка best effort: любая ошибка не должна дойти до запуска
            out.errors.append(_error_text(e, out.conn))
            if i + 1 < attempts and not _permanent(e):
                time.sleep(RETRY_PAUSE)
            else:
                return False
    if out.message.document is not None:
        try:
            bot.send_document(chat, out.message.document)
        except Exception as e:  # noqa: BLE001
            out.errors.append(f"файл {out.message.document.name} не отправлен: {_error_text(e, out.conn)}")
    return True


def _permanent(e: Exception) -> bool:
    """Неверный токен, бот заблокирован, чат не найден: повтор через секунды не поможет."""
    return getattr(e, "code", None) in (400, 401, 403, 404)


def _finish(app: App, out: Outgoing, attempts: int, db: Any | None = None) -> bool:
    ok = False
    try:
        ok = _deliver(app, out, attempts)
    except Exception as e:  # noqa: BLE001
        out.errors.append(_error_text(e, out.conn))
    own = db is None
    if own:
        db = connect(out.db_path)
    try:
        _mark(db, out.run_id, SENT if ok else FAILED)
    finally:
        if own:
            db.close()
    if ok:
        _log(out.run_dir, "info", "Сообщение об итоге отправлено в Telegram.")
    for err in out.errors:
        what = "Telegram" if ok else "Сообщение об итоге в Telegram не отправлено"
        _log(out.run_dir, "warn", f"{what}: {err}")
        try:
            _record_error(app, err)
        except Exception:  # noqa: BLE001
            pass
    return ok


def after_finish(app: App, run_id: str, background: bool = True) -> threading.Thread | bool | None:
    """Отправляет сообщение об итоге закрытого запуска, если оно нужно и его ещё никто не отправил.

    `background`: доставка в потоке (не daemon), возвращается поток. Иначе сразу, одной попыткой:
    True отправлено, False не вышло. None: слать нечего или уже отправляет другой процесс.
    Никогда не бросает: ошибка отправки не влияет на запуск.
    """
    try:
        if not _claim(app.db, run_id):
            return None
        try:
            out = _prepare(app, app.runs.get(run_id))
        except Exception as e:  # noqa: BLE001
            _mark(app.db, run_id, FAILED)
            _log(app.paths.run_dir(run_id), "warn", f"Сообщение об итоге в Telegram не подготовлено: {e}")
            return False
        if out is None:
            _mark(app.db, run_id, NONE)
            return None
        if background:
            t = threading.Thread(target=_finish, args=(app, out, BACKGROUND_ATTEMPTS), name=f"dawg-tg-{run_id}")
            t.start()
            return t
        return _finish(app, out, 1, app.db)
    except Exception:  # noqa: BLE001
        return None


def retry_pending(app: App) -> list[str]:
    """Повтор неотправленных за последние сутки. Останавливается на первой неудаче: Telegram недоступен."""
    now = _now()
    rows = app.db.execute(
        "SELECT id FROM runs WHERE status IN ('ok', 'partial', 'failed') AND finished_at >= ?"
        " AND (notify_state = ? OR (notify_state IS NULL AND finished_at < ?) OR (notify_state = ? AND notify_at < ?))"
        " ORDER BY finished_at LIMIT ?",
        (_iso(now - MAX_AGE), FAILED, _iso(now - UNCLAIMED_AFTER), SENDING, _iso(now - STALE_SENDING), RETRY_LIMIT),
    ).fetchall()
    sent = []
    for row in rows:
        res = after_finish(app, row[0], background=False)
        if res is True:
            sent.append(row[0])
        elif res is False:
            break
    return sent

