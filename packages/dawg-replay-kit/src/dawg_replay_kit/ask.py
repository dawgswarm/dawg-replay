"""Вопрос человеку посреди запуска (docs/contract.md, раздел 8).

    code = kit.ask("Код из SMS на номер ***-12", kind="text", secret=True, timeout_minutes=10)
    ok = kit.ask("Отправить 12 заявок?", kind="confirm")
    city = kit.ask("Какой склад?", kind="choice", choices=["Москва", "Казань"])

В режиме приложения kit пишет событие `ask` и раз в 0,2 секунды смотрит, не
появился ли файл `<answers>/<id>.json`. Приложение пишет его атомарно. Файл
остановки при этом проверяет поток отмены (dawg_replay_kit.cancel): отмена
будит `time.sleep` и превращается в SystemExit, как в любом другом месте скрипта.

Секретный ответ kit удаляет сразу после чтения и добавляет в список
маскируемых значений, в события он не попадает. Обычный ответ остаётся
в файле и попадает в событие `ask_closed`.

Автономный режим: вопрос печатается в stderr, ответ читается из терминала,
секрет через getpass. Срок там не действует: ждём, пока человек ответит.
"""

from __future__ import annotations

import getpass
import json
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

from dawg_replay_kit.events import Emitter

ENV_ANSWERS_DIR = "DAWG_SCRIPT_ANSWERS_DIR"
KINDS = ("text", "confirm", "choice")
OUTCOMES = ("answered", "timeout")
POLL_SECONDS = 0.2
DEFAULT_TIMEOUT_MINUTES = 30
MAX_TIMEOUT_MINUTES = 24 * 60
MIN_MASKED = 3  # короче не маскируем: «1» или «да» испортили бы весь лог
YES = ("yes", "y", "да", "д", "true", "1")
NO = ("no", "n", "нет", "н", "false", "0")


class AskTimeout(Exception):
    """Ответа нет: срок вышел или в автономном режиме ввод закрыт."""


def _minutes(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def parse_confirm(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in YES:
            return True
        if v in NO:
            return False
    return None


def parse_choice(value: Any, choices: Sequence[str]) -> str | None:
    if isinstance(value, str):
        v = value.strip()
        if v in choices:
            return v
        if v.isdigit() and 1 <= int(v) <= len(choices):
            return choices[int(v) - 1]
    return None


class Asker:
    def __init__(
        self,
        events: Emitter,
        answers_dir: Path | None,
        *,
        input_fn: Callable[[], str] | None = None,
        secret_input_fn: Callable[[], str] | None = None,
        stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll: float = POLL_SECONDS,
    ):
        self._events = events
        self._dir = answers_dir
        self._input = input_fn or input
        self._secret_input = secret_input_fn or (lambda: getpass.getpass("> ", stream=self._stream))
        self._stream_override = stream
        self._clock = clock
        self._sleep = sleep
        self._poll = poll
        self._count = 0

    @property
    def _stream(self) -> TextIO:
        return self._stream_override if self._stream_override is not None else sys.stderr

    def ask(
        self,
        question: str,
        kind: str = "text",
        choices: Sequence[str] | None = None,
        secret: bool = False,
        timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES,
    ) -> str | bool:
        question, choices = _check(question, kind, choices, secret, timeout_minutes)
        if self._dir is None:
            return self._ask_terminal(question, kind, choices, secret)
        return self._ask_app(question, kind, choices, secret, timeout_minutes)

    # --- режим приложения ---

    def _ask_app(self, question: str, kind: str, choices: list[str] | None, secret: bool, timeout: float) -> str | bool:
        assert self._dir is not None
        self._count += 1
        ask_id = f"{self._count}-{secrets.token_hex(3)}"
        deadline = datetime.now(timezone.utc) + timedelta(minutes=timeout)
        self._events.emit(
            "ask",
            id=ask_id,
            question=question,
            kind=kind,
            choices=choices,
            secret=secret,
            deadline=deadline.isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        path = self._dir / f"{ask_id}.json"
        until = self._clock() + timeout * 60
        while True:
            value = self._take(path, kind, choices, secret)
            if value is not None:
                if secret and isinstance(value, str) and len(value) >= MIN_MASKED:
                    self._events.add_secret(value)
                self._events.emit("ask_closed", id=ask_id, outcome="answered", answer=None if secret else value)
                if secret:
                    # Удаляем только после ask_closed: без файла и без закрытия вопрос снова выглядел бы открытым,
                    # и приложение приняло бы на него второй ответ.
                    _remove(path)
                return value
            if self._clock() >= until:
                self._events.emit("ask_closed", id=ask_id, outcome="timeout")
                raise AskTimeout(f"не дождались ответа на вопрос «{question}» за {_minutes(timeout)} мин")
            self._sleep(self._poll)

    def _take(self, path: Path, kind: str, choices: list[str] | None, secret: bool) -> str | bool | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None  # файл ещё занят (Windows): прочитаем на следующем круге
        try:
            data = json.loads(raw)
            answer = data["answer"] if isinstance(data, dict) else None
        except (ValueError, KeyError):
            answer = None
        value = _accept(answer, kind, choices)
        if value is None:
            _remove(path)  # неподходящий ответ убираем, чтобы можно было ответить заново
            self._events.log("warn", "ответ на вопрос не подошёл по формату, ждём другой")
        return value

    # --- автономный режим ---

    def _ask_terminal(self, question: str, kind: str, choices: list[str] | None, secret: bool) -> str | bool:
        out = self._stream
        print(f"\nВОПРОС: {question}", file=out, flush=True)
        if kind == "confirm":
            print("Ответьте да или нет.", file=out, flush=True)
        elif kind == "choice":
            assert choices is not None
            for i, c in enumerate(choices, 1):
                print(f"  {i}. {c}", file=out, flush=True)
            print("Номер или текст варианта.", file=out, flush=True)
        while True:
            try:
                if secret:
                    raw = self._secret_input()
                else:
                    print("> ", end="", file=out, flush=True)
                    raw = self._input()
            except EOFError:
                raise AskTimeout(f"ответить на вопрос «{question}» негде: ввод закрыт") from None
            value = _accept(raw, kind, choices)
            if value is not None:
                return value
            print("Ответ не подошёл, попробуйте ещё раз.", file=out, flush=True)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _accept(answer: Any, kind: str, choices: list[str] | None) -> str | bool | None:
    if kind == "confirm":
        return parse_confirm(answer)
    if kind == "choice":
        assert choices is not None
        return parse_choice(answer, choices)
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    return None


def _check(
    question: Any, kind: Any, choices: Any, secret: Any, timeout_minutes: Any
) -> tuple[str, list[str] | None]:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("ask: нужен текст вопроса")
    if kind not in KINDS:
        raise ValueError(f"ask: kind {kind!r}, допустимы: {', '.join(KINDS)}")
    if not isinstance(secret, bool):
        raise TypeError("ask: secret должен быть True или False")
    if secret and kind != "text":
        raise ValueError("ask: secret бывает только у kind='text'")
    if isinstance(timeout_minutes, bool) or not isinstance(timeout_minutes, (int, float)) \
            or not 0 < timeout_minutes <= MAX_TIMEOUT_MINUTES:
        raise ValueError(f"ask: timeout_minutes от 0 до {MAX_TIMEOUT_MINUTES}")
    if kind == "choice":
        if isinstance(choices, str) or not isinstance(choices, Sequence):
            raise ValueError("ask: для kind='choice' нужен список choices")
        items = [str(c).strip() for c in choices]
        if not items or any(not c for c in items) or len(set(items)) != len(items):
            raise ValueError("ask: choices должны быть непустыми и без повторов")
        return question.strip(), items
    if choices is not None:
        raise ValueError("ask: choices нужны только для kind='choice'")
    return question.strip(), None
