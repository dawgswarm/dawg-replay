"""Относительные даты для параметров `date` и `datetime` (контракт 1.3).

В `default` манифеста и в пресете вместо даты можно хранить выражение. Оно
вычисляется в момент запуска по местному времени, скрипт получает готовую дату.

    today, today-1, today+7                   сегодня, вчера, через неделю
    week_start                                понедельник этой недели (ISO)
    month_start, month_end                    первый и последний день этого месяца
    prev_month_start, prev_month_end          первый и последний день прошлого месяца

К любому основанию можно прибавить или отнять дни: `month_start+14`, `week_start-7`.
У `datetime` после выражения может стоять время: `today-1T09:00`; без времени 00:00.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

BASES = ("today", "week_start", "month_start", "month_end", "prev_month_start", "prev_month_end")
_EXPR = r"(?P<base>" + "|".join(BASES) + r")(?P<shift>[+-]\d{1,4})?"
_DATE_RE = re.compile(rf"^{_EXPR}$")
_DATETIME_RE = re.compile(rf"^{_EXPR}(?:T(?P<time>(?:[01]\d|2[0-3]):[0-5]\d))?$")

# Выражения для выбора в форме: самые частые.
COMMON = ("today", "today-1", "today+1", "today+7", "week_start", "month_start", "month_end",
          "prev_month_start", "prev_month_end")

_TITLES = {
    "today": "сегодня",
    "week_start": "начало недели",
    "month_start": "начало месяца",
    "month_end": "конец месяца",
    "prev_month_start": "начало прошлого месяца",
    "prev_month_end": "конец прошлого месяца",
}


def is_relative(value: Any, kind: str = "date") -> bool:
    """Строка с выражением, а не с датой. `kind`: `date` или `datetime`."""
    return isinstance(value, str) and bool((_DATETIME_RE if kind == "datetime" else _DATE_RE).fullmatch(value))


def resolve(value: str, kind: str = "date", today: dt.date | None = None) -> str:
    """Выражение в дату `ГГГГ-ММ-ДД` (или `ГГГГ-ММ-ДДTЧЧ:ММ` для `datetime`). Не выражение: как есть."""
    m = (_DATETIME_RE if kind == "datetime" else _DATE_RE).fullmatch(value) if isinstance(value, str) else None
    if m is None:
        return value
    day = _base(m.group("base"), today or dt.date.today()) + dt.timedelta(days=int(m.group("shift") or 0))
    if kind == "datetime":
        return f"{day.isoformat()}T{m.groupdict().get('time') or '00:00'}"
    return day.isoformat()


def title(value: str) -> str:
    """Выражение словами для формы: «вчера», «начало месяца + 14 дн.», «сегодня, 09:00»."""
    m = _DATETIME_RE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        return value
    base, shift = m.group("base"), int(m.group("shift") or 0)
    if base == "today" and shift in (-1, 1):
        text = "вчера" if shift == -1 else "завтра"
    else:
        text = _TITLES[base] + (f" {'+' if shift > 0 else '−'} {abs(shift)} дн." if shift else "")
    t = m.groupdict().get("time")
    return f"{text}, {t}" if t else text


def _base(base: str, today: dt.date) -> dt.date:
    if base == "today":
        return today
    if base == "week_start":
        return today - dt.timedelta(days=today.weekday())
    first = today.replace(day=1)
    if base == "month_start":
        return first
    if base == "month_end":
        nxt = (first + dt.timedelta(days=32)).replace(day=1)
        return nxt - dt.timedelta(days=1)
    prev_end = first - dt.timedelta(days=1)
    if base == "prev_month_end":
        return prev_end
    return prev_end.replace(day=1)  # prev_month_start
