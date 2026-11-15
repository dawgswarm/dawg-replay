"""Вопросы скрипта (kit.ask) в Telegram: сообщение о вопросе и ответ оттуда.

Процесс, который ведёт запуск (`launch.supervise`), создаёт `RunAsks` (`start`). Он
следит за событиями `ask` и `ask_closed` своего запуска, отправляет сообщение о
вопросе (confirm и choice с кнопками, text с просьбой ответить на сообщение) и
правит его, когда вопрос закрылся. Отправка идёт в своём потоке: цикл запуска и
отмена Telegram не ждут, ошибка становится строкой журнала запуска и «последней
ошибкой» в Настройках.

Связь сообщения с вопросом: файл `<home>/telegram/asks/<key>.json` (`Link`), где
`key` стоит в `callback_data` кнопок, а `message_id` находит вопрос по reply. В
файле процесс-владелец (pid и время старта): если он умер, вопрос закрыт.

Ответы принимает единственный на установку опрашивающий (`dawg_replay.tg_updates`):
процесс, чей запуск ждёт ответа, пытается взять блокировку и, взяв, раскладывает
ответы всем ждущим запускам установки (`Dispatcher`). Пока его вопросы ждут, он
держит блокировку; закрылись — отпускает, и её берёт другой ждущий процесс. Опрос
идёт, только пока есть ждущие вопросы: нажатие на кнопку уже отвеченного вопроса
Telegram хранит до следующего опроса, тогда оно и получит «Уже отвечено».

Ответ пишется `asks.write_answer`, тем же, что ответ из UI и CLI: первый ответ
засчитывается атомарно. Принимаются только сообщения из привязанного чата.
Секретный ответ: бот сразу удаляет сообщение с ним, значение нигде не пишется
(ни в журнал, ни в файлы связей, ни в текст правки).
"""

from __future__ import annotations

import json
import os
import queue
import secrets as _secrets
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dawg_replay import asks, diagnosis, procinfo, tg_updates
from dawg_replay.errors import ReplayError

LINKS_DIR = "asks"
POLL_TIMEOUT = 5  # секунд long polling у getUpdates: на столько может задержаться передача роли
IDLE_SECONDS = 1.0  # пауза опрашивающего без блокировки или без своих вопросов
ERROR_PAUSE = 5.0  # пауза после ошибки getUpdates
SEND_ATTEMPTS = 2
CLOSE_WAIT = 5.0  # сколько ждать отправки последних правок в конце запуска
BUTTON_TEXT = 60

T_ACCEPTED = "Ответ принят"
T_ALREADY = "Уже отвечено"
T_CLOSED = "Вопрос закрыт"
T_TIMEOUT = "Срок ответа вышел"
T_RUN_ENDED = "Вопрос закрыт: запуск завершился"
T_DELETE_YOURSELF = "Не удалось удалить сообщение с кодом, удалите его сами."
T_SEVERAL = "Ответа ждут несколько вопросов: ответьте на сообщение с нужным вопросом (Ответить)."
T_NONE = "Сейчас скрипты ничего не спрашивают."


# --- точки, которые подменяют соседние тикеты и тесты -------------------------------


def wants_ask(app: Any, rec: Any) -> bool:
    """Правило «ask» для запуска. Заглушка до правил уведомлений (tg_rules.wants_ask)."""
    try:
        from dawg_replay import tg_rules  # type: ignore[attr-defined]
    except ImportError:
        return not rec.draft
    return bool(tg_rules.wants_ask(app, rec))


def open_bot(app: Any) -> tuple[Any, int] | None:
    """Бот и привязанный чат, если Telegram подключён."""
    try:
        from dawg_replay import telegram  # type: ignore[attr-defined]
    except ImportError:
        return None
    conn = telegram.connection(app)
    if conn is None:
        return None
    return telegram.Bot(conn.token, conn.api_url), int(conn.chat_id)


def record_error(app: Any, text: str) -> None:
    try:
        from dawg_replay import telegram  # type: ignore[attr-defined]
    except ImportError:
        return
    try:
        telegram.record_error(app, text)
    except Exception:  # отметка ошибки не должна ломать запуск
        pass


def _err(e: BaseException) -> str:
    desc = getattr(e, "description", None)
    return str(desc or e) or type(e).__name__


# --- связи сообщений с вопросами ---------------------------------------------------------


@dataclass
class Link:
    key: str
    run_id: str
    ask_id: str
    chat_id: int
    kind: str
    secret: bool
    text: str  # текст сообщения о вопросе, без кнопок; для правки
    choices: list[str] | None = None
    message_id: int | None = None
    pid: int = field(default_factory=os.getpid)
    pid_started: float | None = None
    state: str = "open"  # open; answered (принят из Telegram); closed (сообщение уже поправлено)


def links_dir(home: Path) -> Path:
    d = tg_updates.telegram_dir(home) / LINKS_DIR
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def save_link(home: Path, link: Link) -> None:
    d = links_dir(home)
    tmp = d / f".{link.key}.{_secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(link), f, ensure_ascii=False)
    os.replace(tmp, d / f"{link.key}.json")


def load_link(home: Path, key: str) -> Link | None:
    if not key.isalnum():
        return None
    try:
        data = json.loads((links_dir(home) / f"{key}.json").read_text(encoding="utf-8"))
        return Link(**data)
    except (OSError, ValueError, TypeError):
        return None


def load_links(home: Path) -> list[Link]:
    out = []
    for p in sorted(links_dir(home).glob("*.json")):
        link = load_link(home, p.stem)
        if link is not None:
            out.append(link)
    return out


def delete_link(home: Path, key: str) -> None:
    try:
        (links_dir(home) / f"{key}.json").unlink()
    except OSError:
        pass


_SELF: dict[int, float | None] = {}


def _self_started() -> float | None:
    pid = os.getpid()
    if pid not in _SELF:
        _SELF[pid] = procinfo.start_time(pid)
    return _SELF[pid]


def owner_alive(link: Link) -> bool:
    """Процесс, который ведёт запуск вопроса, жив: иначе вопрос закрыт."""
    if link.pid == os.getpid() and link.pid_started == _self_started():
        return True
    if link.pid_started is None:  # время старта не узнать: только по pid
        if not link.pid or sys.platform == "win32":
            return False
        try:
            os.kill(link.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return procinfo.same_process(link.pid, link.pid_started)


def link_question(home: Path, link: Link) -> asks.Question | None:
    run_dir = Path(home) / "runs" / link.run_id
    return next((q for q in asks.read(run_dir / "events.jsonl") if q.id == link.ask_id), None)


def link_waiting(home: Path, link: Link) -> bool:
    if not owner_alive(link):
        return False
    q = link_question(home, link)
    return q is not None and asks.is_waiting(Path(home) / "runs" / link.run_id, q)


# --- тексты ------------------------------------------------------------------------------


def _deadline(q: asks.Question) -> str | None:
    dl = q.deadline_dt()
    if dl is None:
        return None
    local = dl.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return local.strftime("%H:%M")
    return local.strftime("%d.%m %H:%M")


def question_text(title: str, preset: str | None, q: asks.Question, hide: list[str] = ()) -> str:
    head = f"{title} · {preset}" if preset else title
    lines = [head, "", f"Вопрос: {q.question}"]
    if q.kind == "choice" and q.choices:
        lines += [f"{i}. {c}" for i, c in enumerate(q.choices, 1)]
    if q.kind == "text":
        lines.append("Чтобы ответить, ответьте на это сообщение (Ответить).")
    else:
        lines.append("Нажмите кнопку с ответом.")
    if q.secret:
        lines.append("Ответ секретный: бот сразу удалит ваше сообщение с ним.")
    dl = _deadline(q)
    if dl:
        lines.append(f"Ответить до {dl}.")
    return diagnosis.scrub("\n".join(lines), hide)


def keyboard(key: str, q: asks.Question) -> dict[str, Any] | None:
    if q.kind == "confirm":
        return {"inline_keyboard": [[{"text": "Да", "callback_data": f"a:{key}:y"},
                                     {"text": "Нет", "callback_data": f"a:{key}:n"}]]}
    if q.kind == "choice" and q.choices:
        rows = []
        for i, c in enumerate(q.choices, 1):
            label = c if len(c) <= BUTTON_TEXT else c[: BUTTON_TEXT - 1] + "…"
            rows.append([{"text": f"{i}. {label}", "callback_data": f"a:{key}:{i}"}])
        return {"inline_keyboard": rows}
    return None


def closed_text(link: Link, note: str, value: Any = None) -> str:
    shown = "" if link.secret or value is None else f": {asks.answer_text(value)}"
    return f"{link.text}\n\n{note}{shown}"


# --- раскладка ответов ---------------------------------------------------------------------


class Dispatcher:
    """Обрабатывает обновления Telegram: ответы на вопросы любых запусков установки."""

    def __init__(self, home: Path, bot: Any, chat_id: int, report: Callable[[str], None]):
        self.home = Path(home)
        self.bot = bot
        self.chat_id = int(chat_id)
        self.report = report

    def handle(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            self._callback(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            self._message(update["message"])

    def _chat(self, msg: Any) -> int | None:
        chat = msg.get("chat") if isinstance(msg, dict) else None
        cid = chat.get("id") if isinstance(chat, dict) else None
        return cid if isinstance(cid, int) else None

    def _callback(self, cq: dict[str, Any]) -> None:
        if self._chat(cq.get("message")) != self.chat_id:
            return  # чужой чат: ни ответа, ни подсказки
        cq_id = cq.get("id")
        data = cq.get("data")
        parts = data.split(":") if isinstance(data, str) else []
        link = load_link(self.home, parts[1]) if len(parts) == 3 and parts[0] == "a" else None
        if link is None or link.chat_id != self.chat_id:
            self._answer_cb(cq_id, T_CLOSED)
            return
        raw: Any = parts[2]
        if link.kind == "confirm":
            raw = {"y": True, "n": False}.get(parts[2], "")
        status, info = self.accept(link, raw)
        self._answer_cb(cq_id, info if status == "bad" else _STATUS_TEXT[status])

    def _message(self, msg: dict[str, Any]) -> None:
        if self._chat(msg) != self.chat_id:
            return
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip() or text.startswith("/"):
            return
        links = [x for x in load_links(self.home) if x.chat_id == self.chat_id]
        link = None
        reply = msg.get("reply_to_message")
        if isinstance(reply, dict) and reply.get("message_id") is not None:
            link = next((x for x in links if x.message_id == reply.get("message_id")), None)
        if link is None:
            waiting = [x for x in links if link_waiting(self.home, x)]
            if len(waiting) != 1:
                # Похоже на код, а куда его отнести, непонятно: всё равно убираем из чата.
                maybe_secret = any(x.secret and owner_alive(x) for x in links)
                tail = self._delete(msg) if maybe_secret else ""
                self._send(T_SEVERAL if waiting else T_NONE, tail)
                return
            link = waiting[0]
        tail = self._delete(msg) if link.secret else ""
        status, info = self.accept(link, text)
        if status == "ok":
            if tail:
                self._send(T_ACCEPTED + ".", tail)
            return
        self._send(info if status == "bad" else _STATUS_TEXT[status] + ".", tail)

    def accept(self, link: Link, raw: Any) -> tuple[str, str]:
        """Пишет ответ. Возвращает (ok|already|closed|bad, подсказка для bad)."""
        if not owner_alive(link):
            return "closed", ""
        run_dir = self.home / "runs" / link.run_id
        q = link_question(self.home, link)
        if q is None:
            return "closed", ""
        if q.outcome == "answered" or asks.has_answer(run_dir, q.id):
            return "already", ""
        if q.outcome is not None or q.expired():
            return "closed", ""
        try:
            value = asks.normalize(q, raw)
            asks.write_answer(run_dir, q, value)
        except ReplayError as e:
            if e.code == "already_answered":
                return "already", ""
            return "bad", e.message
        link.state = "answered"
        save_link(self.home, link)
        if link.message_id is not None:
            try:
                self.bot.edit_message_text(self.chat_id, link.message_id, closed_text(link, T_ACCEPTED, value))
            except Exception as e:
                self.report(f"не удалось поправить сообщение о вопросе: {_err(e)}")
        return "ok", ""

    def _delete(self, msg: dict[str, Any]) -> str:
        """Удаляет сообщение пользователя. Не вышло: текст просьбы удалить самому."""
        try:
            if self.bot.delete_message(self.chat_id, msg.get("message_id")):
                return ""
        except Exception as e:
            self.report(f"не удалось удалить сообщение с кодом: {_err(e)}")
        return T_DELETE_YOURSELF

    def _send(self, text: str, tail: str = "") -> None:
        try:
            self.bot.send_message(self.chat_id, f"{text} {tail}".strip())
        except Exception as e:
            self.report(f"не удалось отправить ответ в чат: {_err(e)}")

    def _answer_cb(self, cq_id: Any, text: str | None) -> None:
        try:
            self.bot.answer_callback_query(cq_id, text)
        except Exception as e:
            self.report(f"не удалось ответить на нажатие кнопки: {_err(e)}")


_STATUS_TEXT = {"ok": T_ACCEPTED, "already": T_ALREADY, "closed": T_CLOSED}


# --- запуск ------------------------------------------------------------------------------------


class RunAsks:
    """Вопросы одного запуска в Telegram. `tick` и `close` зовёт цикл `supervise` (поток с БД),
    сеть только в своих потоках."""

    def __init__(self, home: Path, run_id: str, bot: Any, chat_id: int, *, title: str,
                 preset: str | None = None, hide: list[str] = ()):
        self.home = Path(home)
        self.run_id = run_id
        self.run_dir = self.home / "runs" / run_id
        self.bot = bot
        self.chat_id = int(chat_id)
        self.title = title
        self.preset = preset
        self._hide = list(hide)
        self._pid_started = _self_started()
        self._found: dict[str, asks.Question] = {}
        self._pos = 0
        self._pending = b""
        self._links: dict[str, str] = {}  # ask_id -> key
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._jobs: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._worker = threading.Thread(target=self._work, name=f"dawg-tg-{run_id}", daemon=True)
        self._worker.start()
        self._poll_stop = threading.Event()
        self._poller: threading.Thread | None = None
        self.dispatcher = Dispatcher(self.home, bot, self.chat_id, self.report)

    # --- из цикла запуска ---

    def tick(self) -> list[str]:
        """Новые события запуска. Возвращает накопленные ошибки отправки (их пишет вызывающий)."""
        for e in self._new_events():
            q = asks.apply(self._found, e)
            if q is None:
                continue
            if e.get("t") == "ask":
                self._jobs.put(lambda q=q: self._send(q))
            else:
                self._jobs.put(lambda q=q: self._closed(q))
        return self.take_errors()

    def close(self, wait: float = CLOSE_WAIT) -> list[str]:
        """Запуск закончился: правит открытые вопросы, удаляет связи, гасит опрос."""
        self.tick()
        self._poll_stop.set()
        self._jobs.put(self._finish)
        self._jobs.put(None)
        self._worker.join(wait)
        if self._poller is not None:
            self._poller.join(0.1)
        return self.take_errors()

    def report(self, text: str) -> None:
        with self._lock:
            self._errors.append(text)

    def take_errors(self) -> list[str]:
        with self._lock:
            out, self._errors = self._errors, []
        return out

    def _new_events(self) -> list[dict[str, Any]]:
        try:
            with open(self.run_dir / "events.jsonl", "rb") as f:
                f.seek(self._pos)
                chunk = f.read()
        except OSError:
            return []
        self._pos += len(chunk)
        data = self._pending + chunk
        lines = data.split(b"\n")
        self._pending = lines.pop()  # неполная последняя строка
        out = []
        for raw in lines:
            if asks._MARK not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out

    # --- в потоке отправки ---

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception as e:  # отправка best effort
                self.report(_err(e))

    def _retry(self, what: str, fn: Callable[[], Any]) -> Any:
        last: BaseException | None = None
        for _ in range(SEND_ATTEMPTS):
            try:
                return fn()
            except Exception as e:
                last = e
        self.report(f"{what}: {_err(last)}" if last else what)
        return None

    def _send(self, q: asks.Question) -> None:
        if not asks.is_waiting(self.run_dir, q):
            return  # уже ответили в приложении или срок вышел
        key = _secrets.token_hex(5)
        link = Link(key=key, run_id=self.run_id, ask_id=q.id, chat_id=self.chat_id, kind=q.kind,
                    secret=q.secret, text=question_text(self.title, self.preset, q, self._hide),
                    choices=q.choices, pid_started=self._pid_started)
        save_link(self.home, link)  # до отправки: кнопку могут нажать сразу
        mid = self._retry("не удалось отправить вопрос в Telegram",
                          lambda: self.bot.send_message(self.chat_id, link.text, keyboard(key, q)))
        if mid is None:
            delete_link(self.home, key)
            return
        current = load_link(self.home, key) or link
        current.message_id = mid
        save_link(self.home, current)
        self._links[q.id] = key
        self._start_poller()

    def _closed(self, q: asks.Question) -> None:
        key = self._links.get(q.id)
        link = load_link(self.home, key) if key else None
        if link is None or link.state != "open" or link.message_id is None:
            return
        if q.outcome == "answered":
            text = closed_text(link, T_ACCEPTED, q.answer)
        else:
            text = closed_text(link, T_TIMEOUT)
        link.state = "closed"
        save_link(self.home, link)
        self._retry("не удалось поправить сообщение о вопросе",
                    lambda: self.bot.edit_message_text(self.chat_id, link.message_id, text))

    def _finish(self) -> None:
        for key in list(self._links.values()):
            link = load_link(self.home, key)
            if link is not None and link.state == "open" and link.message_id is not None:
                self._retry("не удалось поправить сообщение о вопросе",
                            lambda: self.bot.edit_message_text(self.chat_id, link.message_id,
                                                               closed_text(link, T_RUN_ENDED)))
            delete_link(self.home, key)
        self._links.clear()

    # --- опрос ---

    def own_waiting(self) -> bool:
        for key in list(self._links.values()):
            link = load_link(self.home, key)
            if link is not None and link.state == "open" and link_waiting(self.home, link):
                return True
        return False

    def _start_poller(self) -> None:
        if self._poller is None or not self._poller.is_alive():
            self._poller = threading.Thread(target=self._poll, name=f"dawg-tg-poll-{self.run_id}", daemon=True)
            self._poller.start()

    def _poll(self) -> None:
        lock = tg_updates.UpdatesLock(self.home)
        try:
            while not self._poll_stop.is_set():
                if not self.own_waiting():
                    lock.release()  # пусть привязка чата или другой запуск не ждут
                    self._poll_stop.wait(IDLE_SECONDS)
                    continue
                if not lock.acquire(0):
                    self._poll_stop.wait(IDLE_SECONDS)
                    continue
                try:
                    updates = self.bot.get_updates(tg_updates.load_offset(self.home), POLL_TIMEOUT)
                except Exception as e:
                    self.report(f"не удалось получить ответы из Telegram: {_err(e)}")
                    self._poll_stop.wait(ERROR_PAUSE)
                    continue
                for u in updates or []:
                    try:
                        self.dispatcher.handle(u)
                    except Exception as e:
                        self.report(f"ответ из Telegram не разобран: {_err(e)}")
                    uid = u.get("update_id") if isinstance(u, dict) else None
                    if isinstance(uid, int):
                        tg_updates.save_offset(self.home, uid + 1)
        finally:
            lock.release()


def start(app: Any, rec: Any, payload: dict[str, Any] | None = None) -> RunAsks | None:
    """Единственная точка из `supervise`: RunAsks, если Telegram подключён и правило ask включено."""
    try:
        if not wants_ask(app, rec):
            return None
        opened = open_bot(app)
        if opened is None:
            return None
        bot, chat_id = opened
        try:
            title = app.registry.get(rec.script_name).title or rec.script_name
        except ReplayError:
            title = rec.script_name
        hide = diagnosis.known_secrets(app, rec.script_name)
        payload = payload or {}
        values = payload.get("values") or {}
        hide += [str(values[n]) for n in payload.get("secret_names") or () if values.get(n)]
        return RunAsks(app.paths.home, rec.id, bot, chat_id, title=title, preset=rec.preset, hide=hide)
    except Exception as e:  # Telegram не должен мешать запуску
        record_error(app, f"вопросы в Telegram: {_err(e)}")
        return None


def journal(run_dir: Path, texts: list[str]) -> None:
    """Ошибки Telegram строкой в журнал запуска (событие log уровня warn)."""
    for text in texts:
        line = json.dumps({"t": "log", "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                           .replace("+00:00", "Z"), "level": "warn", "msg": f"Telegram: {text}"},
                          ensure_ascii=False) + "\n"
        try:
            fd = os.open(Path(run_dir) / "events.jsonl", os.O_WRONLY | os.O_APPEND)
        except OSError:
            return
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
