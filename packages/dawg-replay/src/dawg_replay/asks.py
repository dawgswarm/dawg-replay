"""Вопросы скрипта посреди запуска (kit.ask, docs/contract.md раздел 8): чтение и ответ.

Скрипт пишет событие `ask` и ждёт файл `<run_dir>/answers/<id>.json`. Получив
ответ или не дождавшись его, пишет `ask_closed`. Вопрос ждёт ответа, пока
запуск идёт, `ask_closed` нет, файла ответа нет и срок не вышел.

Ответ пишется атомарно (временный файл и жёсткая ссылка), с правами 0600,
засчитывается первый. Секретный
ответ скрипт удаляет сразу после чтения; если скрипт не успел, файл удаляет
`drop_secret_answers` после выхода процесса. В БД, историю и логи приложения
ответ не попадает.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dawg_replay_kit.ask import KINDS, parse_choice, parse_confirm

from dawg_replay.errors import ReplayError

ANSWERS_DIR = "answers"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MARK = b'"ask'  # быстрый отсев строк: события ask и ask_closed


@dataclass
class Question:
    id: str
    question: str
    kind: str
    choices: list[str] | None
    secret: bool
    deadline: str
    ts: str | None = None
    outcome: str | None = None  # answered, timeout; None пока открыт
    answer: Any = None  # только несекретный, из ask_closed
    closed_ts: str | None = None

    def deadline_dt(self) -> datetime | None:
        try:
            dt = datetime.fromisoformat(self.deadline.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def expired(self, now: datetime | None = None) -> bool:
        dl = self.deadline_dt()
        return dl is not None and dl <= (now or datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "question": self.question, "kind": self.kind, "choices": self.choices,
            "secret": self.secret, "deadline": self.deadline, "ts": self.ts, "outcome": self.outcome,
            "answer": None if self.secret else self.answer,
        }


def parse_ask(e: dict[str, Any]) -> Question:
    """Событие `ask` в Question. Неподходящее событие: ValueError."""
    ask_id = e.get("id")
    if not isinstance(ask_id, str) or not _ID_RE.fullmatch(ask_id):
        raise ValueError("id вопроса")
    question = e.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("текст вопроса")
    kind = e.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind {kind!r}")
    choices = e.get("choices")
    if kind == "choice":
        if not isinstance(choices, list) or not choices:
            raise ValueError("choices")
        choices = [str(c) for c in choices]
    else:
        choices = None
    deadline = e.get("deadline")
    if not isinstance(deadline, str):
        raise ValueError("deadline")
    return Question(ask_id, question, kind, choices, bool(e.get("secret")), deadline, ts=e.get("ts"))


def answer_path(run_dir: Path, ask_id: str) -> Path:
    if not isinstance(ask_id, str) or not _ID_RE.fullmatch(ask_id):
        raise ReplayError("bad_value", f"недопустимый id вопроса: {ask_id!r}")
    return Path(run_dir) / ANSWERS_DIR / f"{ask_id}.json"


def has_answer(run_dir: Path, ask_id: str) -> bool:
    try:
        return answer_path(run_dir, ask_id).exists()
    except ReplayError:
        return False


def is_waiting(run_dir: Path, q: Question, now: datetime | None = None) -> bool:
    """Вопрос ждёт ответа (без учёта того, идёт ли запуск)."""
    return q.outcome is None and not q.expired(now) and not has_answer(run_dir, q.id)


def read(events_path: Path) -> list[Question]:
    """Все вопросы из файла событий в порядке появления."""
    found: dict[str, Question] = {}
    try:
        f = open(events_path, "rb")
    except OSError:
        return []
    with f:
        for raw in f:
            if _MARK not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict):
                apply(found, e)
    return list(found.values())


def apply(found: dict[str, Question], e: dict[str, Any]) -> Question | None:
    """Учитывает событие `ask` или `ask_closed`. Возвращает затронутый вопрос."""
    t = e.get("t")
    if t == "ask":
        try:
            q = parse_ask(e)
        except ValueError:
            return None
        if q.id in found:
            return None
        found[q.id] = q
        return q
    if t == "ask_closed":
        q = found.get(str(e.get("id")))
        if q is None or q.outcome is not None:
            return None
        q.outcome = "answered" if e.get("outcome") == "answered" else "timeout"
        q.closed_ts = e.get("ts")
        if not q.secret and "answer" in e:
            q.answer = e["answer"]
        return q
    return None


def waiting(run_dir: Path, now: datetime | None = None) -> list[Question]:
    run_dir = Path(run_dir)
    return [q for q in read(run_dir / "events.jsonl") if is_waiting(run_dir, q, now)]


def normalize(q: Question, raw: Any) -> str | bool:
    """Ответ человека к виду, который ждёт скрипт. Не подходит: ReplayError с подсказкой."""
    if q.kind == "confirm":
        value = parse_confirm(raw)
        if value is None:
            raise ReplayError("bad_answer", "ответ на этот вопрос: yes или no (да или нет)")
        return value
    if q.kind == "choice":
        assert q.choices is not None
        value = parse_choice(raw, q.choices)
        if value is None:
            options = ", ".join(f"{i}. {c}" for i, c in enumerate(q.choices, 1))
            raise ReplayError("bad_answer", f"выберите один из вариантов (номер или текст): {options}")
        return value
    if not isinstance(raw, str) or not raw.strip():
        raise ReplayError("bad_answer", "пустой ответ")
    return raw.strip()


def write_answer(run_dir: Path, q: Question, value: str | bool) -> Path:
    """Пишет ответ атомарно, права 0600. Ответ уже есть: ReplayError `already_answered`.

    Первый ответ засчитывается атомарно между UI, CLI и Telegram (разные процессы):
    готовый временный файл ставится на место жёсткой ссылкой, которая не создаётся,
    если файл ответа уже есть. Без жёстких ссылок (редкая ФС) место занимает файл
    `.<id>.claim`, созданный с O_EXCL.
    """
    path = answer_path(run_dir, q.id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise _already()
    tmp = path.with_name(f".{q.id}.{secrets.token_hex(4)}.tmp")
    data = {"id": q.id, "answer": value, "secret": q.secret,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        try:
            os.link(tmp, path)
        except FileExistsError:
            raise _already() from None
        except OSError:
            _claim(path)
            os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


def _already() -> ReplayError:
    return ReplayError("already_answered", "на этот вопрос уже ответили, скрипт скоро продолжит")


def _claim(path: Path) -> None:
    try:
        os.close(os.open(path.with_name(f".{path.stem}.claim"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        raise _already() from None


def pick(run_dir: Path, ask_id: str | None = None) -> Question:
    """Вопрос, на который сейчас можно ответить: по id или единственный ждущий."""
    questions = read(Path(run_dir) / "events.jsonl")
    if ask_id is not None:
        q = next((x for x in questions if x.id == ask_id), None)
        if q is None:
            raise ReplayError("no_question", f"вопроса `{ask_id}` в этом запуске нет")
        if q.outcome == "timeout" or (q.outcome is None and q.expired()):
            raise ReplayError("no_question", "срок ответа на этот вопрос вышел")
        if q.outcome == "answered" or has_answer(run_dir, q.id):
            raise ReplayError("already_answered", "на этот вопрос уже ответили")
        return q
    open_ = [q for q in questions if is_waiting(run_dir, q)]
    if not open_:
        raise ReplayError("no_question", "запуск сейчас ничего не спрашивает")
    if len(open_) > 1:
        ids = ", ".join(f"{q.id} ({q.question})" for q in open_)
        raise ReplayError("several_questions", f"запуск ждёт несколько ответов, укажите --id: {ids}",
                          {"questions": [q.to_dict() for q in open_]})
    return open_[0]


def answer(app: Any, run_id: str, raw: Any, ask_id: str | None = None) -> tuple[Question, str | bool]:
    """Ответ на вопрос идущего запуска. Возвращает вопрос и принятое значение."""
    rec = app.runs.get(run_id)
    if not rec.running:
        raise ReplayError("not_running", f"запуск {run_id} уже завершён ({rec.label}), отвечать некому")
    run_dir = app.paths.run_dir(run_id)
    q = pick(run_dir, ask_id)
    value = normalize(q, raw)
    write_answer(run_dir, q, value)
    return q, value


def drop_secret_answers(run_dir: Path) -> None:
    """Удаляет секретные ответы, которые скрипт не успел прочитать (выход, отмена, падение)."""
    d = Path(run_dir) / ANSWERS_DIR
    try:
        files = list(d.iterdir())
    except OSError:
        return
    for p in files:
        try:
            if p.name.endswith((".tmp", ".claim")):
                p.unlink()
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("secret"):
                p.unlink()
        except (OSError, ValueError):
            continue


def timed_out(run_dir: Path) -> Question | None:
    """Последний вопрос запуска остался без ответа до срока (kit.AskTimeout).

    Вместе с ненулевым выходом это значит: скрипт не сломан, человек не ответил.
    """
    questions = read(Path(run_dir) / "events.jsonl")
    if questions and questions[-1].outcome == "timeout":
        return questions[-1]
    return None


def timeout_text(q: Question) -> str:
    return f"Не дождались ответа на вопрос «{q.question}»."


def answer_text(value: Any) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    return "" if value is None else str(value)
