"""Контракт 1.3: enum с подписями, multi_enum, time, datetime, относительные даты."""

import datetime as dt
import textwrap

import pytest

from dawg_replay_kit import relative
from dawg_replay_kit.manifest import ManifestError, Param, check_value, parse_manifest
from dawg_replay_kit.parameters import ParamsError, from_cli, resolve

SCRIPT = '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["dawg-replay-kit>=1.3,<2"]\n# ///\n'
SUNDAY = dt.date(2026, 11, 15)


def manifest(params: str, contract: str = '"1.3"') -> str:
    body = textwrap.dedent(f"""\
        contract = {contract}
        name = "m4"
        title = "M4"
        version = 1
        """) + textwrap.dedent(params)
    lines = "\n".join(f"# {x}" if x else "#" for x in body.strip("\n").splitlines())
    return f"{SCRIPT}\n# /// dawg-script\n{lines}\n# ///\n"


def codes(text):
    with pytest.raises(ManifestError) as e:
        parse_manifest(text)
    return [(i.code, i.path) for i in e.value.issues]


PARAMS = """
[[params]]
name = "sort"
type = "enum"
title = "Сортировка"
default = "price"
choices = [{ value = "popular", title = "Популярные" }, { value = "price", title = "Дешевле" }, "rating"]

[[params]]
name = "stores"
type = "multi_enum"
title = "Склады"
choices = [{ value = "msk", title = "Москва" }, { value = "kzn", title = "Казань" }, { value = "spb", title = "Петербург" }]
min_items = 1
max_items = 2
default = ["msk"]

[[params]]
name = "at"
type = "time"
title = "Время"
min = "08:00"
max = "20:00"

[[params]]
name = "since"
type = "date"
title = "С даты"
default = "today-1"

[[params]]
name = "moment"
type = "datetime"
title = "Момент"
default = "month_startT09:30"
min = "2026-01-01T00:00"
"""


# --- относительные даты --------------------------------------------------------


@pytest.mark.parametrize("expr, today, want", [
    ("today", SUNDAY, "2026-11-15"),
    ("today-1", SUNDAY, "2026-11-14"),
    ("today+7", SUNDAY, "2026-11-22"),
    ("week_start", SUNDAY, "2026-11-09"),  # воскресенье: понедельник той же недели
    ("week_start", dt.date(2026, 11, 9), "2026-11-09"),
    ("month_start", SUNDAY, "2026-11-01"),
    ("month_end", SUNDAY, "2026-11-30"),
    ("month_end", dt.date(2028, 2, 3), "2028-02-29"),
    ("prev_month_start", SUNDAY, "2026-10-01"),
    ("prev_month_end", SUNDAY, "2026-10-31"),
    ("prev_month_start", dt.date(2027, 1, 10), "2026-12-01"),
    ("prev_month_end", dt.date(2027, 3, 31), "2027-02-28"),
    ("month_start+14", SUNDAY, "2026-11-15"),
    ("2026-01-05", SUNDAY, "2026-01-05"),  # не выражение: как есть
])
def test_relative_dates(expr, today, want):
    assert relative.resolve(expr, "date", today) == want


def test_relative_datetime_and_titles():
    assert relative.resolve("today-1T09:00", "datetime", SUNDAY) == "2026-11-14T09:00"
    assert relative.resolve("today", "datetime", SUNDAY) == "2026-11-15T00:00"
    assert not relative.is_relative("today-1T09:00", "date") and relative.is_relative("today-1T09:00", "datetime")
    assert not relative.is_relative("yesterday") and not relative.is_relative("today-12345")
    assert [relative.title(x) for x in ("today", "today-1", "today+1", "today+7", "month_start+14", "prev_month_end", "today-1T09:00")] == [
        "сегодня", "вчера", "завтра", "сегодня + 7 дн.", "начало месяца + 14 дн.", "конец прошлого месяца", "вчера, 09:00",
    ]


# --- манифест ------------------------------------------------------------------


def test_manifest_m4():
    m = parse_manifest(manifest(PARAMS))
    sort, stores, at, since, moment = m.params
    assert sort.choices == ("popular", "price", "rating") and sort.choice_title("price") == "Дешевле"
    assert sort.choice_title("rating") == "rating"
    assert stores.choices == ("msk", "kzn", "spb") and (stores.min_items, stores.max_items) == (1, 2)
    assert since.default == "today-1" and moment.default == "month_startT09:30"
    assert sort.to_dict()["choices"][0] == {"value": "popular", "title": "Популярные"}
    plain = parse_manifest(manifest('[[params]]\nname = "e"\ntype = "enum"\ntitle = "E"\nchoices = ["a", "b"]\n', contract="1"))
    assert plain.params[0].choice_titles is None and plain.params[0].to_dict()["choices"] == ["a", "b"]


def test_needs_contract_13():
    got = set(codes(manifest(PARAMS, contract='"1.2"')))
    assert got == {
        ("type_needs_minor", "params[1].type"), ("type_needs_minor", "params[2].type"), ("type_needs_minor", "params[4].type"),
        ("field_needs_minor", "params[0].choices"), ("field_needs_minor", "params[1].choices"),
        ("field_needs_minor", "params[1].min_items"), ("field_needs_minor", "params[3].default"),
        ("field_needs_minor", "params[4].default"),
    }


@pytest.mark.parametrize("param, code, key", [
    ('type = "enum"\nchoices = [{ value = "a" }]', "bad_value", "choices"),
    ('type = "enum"\nchoices = [{ value = "a", title = "A", hint = "x" }]', "bad_value", "choices"),
    ('type = "enum"\nchoices = [{ value = "a", title = "A" }, "a"]', "bad_value", "choices"),
    ('type = "multi_enum"', "missing_field", "choices"),
    ('type = "multi_enum"\nchoices = ["a", "b"]\ndefault = ["a", "c"]', "bad_default", "default"),
    ('type = "multi_enum"\nchoices = ["a", "b"]\ndefault = "a"', "bad_default", "default"),
    ('type = "multi_enum"\nchoices = ["a", "b"]\nmin_items = 2\nmax_items = 1', "bad_range", "min_items"),
    ('type = "string_list"\nmin_items = 1', "field_not_allowed", "min_items"),
    ('type = "time"\nmin = "8:00"', "bad_value", "min"),
    ('type = "time"\nmin = "20:00"\nmax = "08:00"', "bad_range", "min"),
    ('type = "time"\ndefault = "24:00"', "bad_default", "default"),
    ('type = "datetime"\ndefault = "2026-11-15 09:00"', "bad_default", "default"),
    ('type = "datetime"\nmin = "today"', "bad_value", "min"),
    ('type = "date"\ndefault = "yesterday"', "bad_default", "default"),
    ('type = "string"\nchoices = ["a"]', "field_not_allowed", "choices"),
])
def test_param_errors(param, code, key):
    assert codes(manifest(f'[[params]]\nname = "x"\ntitle = "X"\n{param}')) == [(code, f"params[0](x).{key}")]


def test_check_value():
    me = Param("s", "multi_enum", "S", choices=("a", "b", "c"), min_items=1, max_items=2)
    assert check_value(me, ["a", "c"]) is None
    assert "нет в choices" in check_value(me, ["a", "z"])
    assert "повторяются" in check_value(me, ["a", "a"])
    assert "max_items" in check_value(me, ["a", "b", "c"])
    assert "min_items" in check_value(me, [])
    t = Param("t", "time", "T", min="08:00", max="20:00")
    assert check_value(t, "09:30") is None and "раньше min" in check_value(t, "07:59") and "ЧЧ:ММ" in check_value(t, "9:30")
    d = Param("d", "datetime", "D", max="2026-12-31T23:59")
    assert check_value(d, "2026-11-15T09:00") is None and check_value(d, "today-1T08:00") is None
    assert "такой даты нет" in check_value(d, "2026-02-30T09:00") and "позже max" in check_value(d, "2027-01-01T00:00")
    assert check_value(Param("x", "date", "X", min="2026-01-01"), "today-1") is None  # min проверяется после вычисления


# --- значения при запуске ----------------------------------------------------------


def test_resolve_computes_relative_dates():
    m = parse_manifest(manifest(PARAMS))
    p = resolve(m, {"at": "09:00"}, "тест", today=SUNDAY)
    assert p.since == "2026-11-14" and p.moment == "2026-11-01T09:30" and p.stores == ["msk"] and p.sort == "price"
    p = resolve(m, {"since": "prev_month_end", "stores": ["kzn", "spb"]}, "тест", today=SUNDAY)
    assert p.since == "2026-10-31" and p.stores == ["kzn", "spb"]
    early = parse_manifest(manifest('[[params]]\nname = "d"\ntype = "date"\ntitle = "D"\nmin = "2026-11-10"\n'))
    with pytest.raises(ParamsError, match="раньше min"):
        resolve(early, {"d": "prev_month_start"}, "тест", today=SUNDAY)
    with pytest.raises(ParamsError, match="max_items"):
        resolve(m, {"stores": ["msk", "kzn", "spb"]}, "тест", today=SUNDAY)


def test_from_cli():
    m = parse_manifest(manifest(PARAMS))
    p = from_cli(m, ["--stores", "msk,kzn", "--sort", "rating", "--at", "10:15", "--since", "week_start",
                     "--moment", "2026-11-20T18:00"], {})
    assert p.stores == ["msk", "kzn"] and p.sort == "rating" and p.at == "10:15"
    assert p.since == relative.resolve("week_start") and p.moment == "2026-11-20T18:00"
    assert from_cli(m, ["--stores", "kzn", "--stores", "spb"], {}).stores == ["kzn", "spb"]
    with pytest.raises(ParamsError, match="нет в choices"):
        from_cli(m, ["--stores", "msk,nsk"], {})
    with pytest.raises(ParamsError, match="max_items"):
        from_cli(m, ["--stores", "msk,kzn,spb"], {})
