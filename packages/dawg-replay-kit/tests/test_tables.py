"""Контракт 1.2: параметры date и table, чтение CSV и XLSX, автономный режим."""

import datetime as dt
import io
import sys
import textwrap

import pytest

from dawg_replay_kit.events import Emitter
from dawg_replay_kit.manifest import ManifestError, Param, check_value, parse_manifest
from dawg_replay_kit.parameters import ParamsError, from_cli, resolve
from dawg_replay_kit.tables import TableError, cell_text, parse_table, read_table

SCRIPT = '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["dawg-replay-kit>=1.2,<2"]\n# ///\n'


def manifest(params: str, contract: str = '"1.2"') -> str:
    body = textwrap.dedent(f"""\
        contract = {contract}
        name = "acts"
        title = "Действия"
        version = 1
        """) + textwrap.dedent(params)
    lines = "\n".join(f"# {x}" if x else "#" for x in body.strip("\n").splitlines())
    return f"{SCRIPT}\n# /// dawg-script\n{lines}\n# ///\n"


TABLE = """
[[params]]
name = "accounts"
type = "table"
title = "Аккаунты"
required = true
columns = ["email", "Сумма"]
max_rows = 3

[[params]]
name = "since"
type = "date"
title = "С даты"
min = "2026-01-01"
max = "2026-12-31"
default = "2026-11-01"
"""


def codes(text):
    with pytest.raises(ManifestError) as e:
        parse_manifest(text)
    return [(i.code, i.path) for i in e.value.issues]


# --- манифест -------------------------------------------------------------------


def test_manifest_with_date_and_table():
    m = parse_manifest(manifest(TABLE))
    acc, since = m.params
    assert (acc.columns, acc.max_rows, acc.secret, acc.row_limit) == (("email", "Сумма"), 3, False, 3)
    assert since.default == "2026-11-01" and since.min == "2026-01-01"
    assert acc.to_dict() == {"name": "accounts", "type": "table", "title": "Аккаунты", "required": True,
                             "max_rows": 3, "columns": ["email", "Сумма"]}
    assert Param("t", "table", "T").row_limit == 10000


def test_new_types_need_contract_12():
    assert codes(manifest(TABLE, contract='"1.1"')) == [
        ("type_needs_minor", "params[0].type"), ("type_needs_minor", "params[1].type"),
    ]
    assert codes(manifest(TABLE, contract="1")) == [
        ("type_needs_minor", "params[0].type"), ("type_needs_minor", "params[1].type"),
    ]


@pytest.mark.parametrize("param, code, key", [
    ('type = "date"\ndefault = "2026-02-30"', "bad_default", "default"),
    ('type = "date"\ndefault = "01.11.2026"', "bad_default", "default"),
    ('type = "date"\nmin = 5', "bad_value", "min"),
    ('type = "date"\nmin = "2026-12-01"\nmax = "2026-01-01"', "bad_range", "min"),
    ('type = "date"\ndefault = "2025-12-31"\nmin = "2026-01-01"', "bad_default", "default"),
    ('type = "table"\ndefault = []', "field_not_allowed", "default"),
    ('type = "table"\ncolumns = "email"', "bad_value", "columns"),
    ('type = "table"\ncolumns = ["a", "a"]', "bad_value", "columns"),
    ('type = "table"\ncolumns = ["_row"]', "bad_value", "columns"),
    ('type = "table"\ncolumns = [" a"]', "bad_value", "columns"),
    ('type = "table"\nmax_rows = 0', "bad_value", "max_rows"),
    ('type = "table"\nsecret = "yes"', "bad_value", "secret"),
    ('type = "table"\nmin = 1', "field_not_allowed", "min"),
    ('type = "string"\ncolumns = ["a"]', "field_not_allowed", "columns"),
    ('type = "string"\nmax_rows = 5', "field_not_allowed", "max_rows"),
    ('type = "string"\nsecret = true', "field_not_allowed", "secret"),
])
def test_param_errors(param, code, key):
    assert codes(manifest(f'[[params]]\nname = "x"\ntitle = "X"\n{param}')) == [(code, f"params[0](x).{key}")]


def test_check_value_date_and_table():
    d = Param("d", "date", "D", min="2026-01-01", max="2026-12-31")
    assert check_value(d, "2026-11-15") is None
    assert "ГГГГ-ММ-ДД" in check_value(d, "15.11.2026")
    assert "такой даты нет" in check_value(d, "2026-02-30")
    assert "раньше min" in check_value(d, "2025-12-31")
    assert "позже max" in check_value(d, "2027-01-01")

    t = Param("t", "table", "T", columns=("email",), max_rows=2)
    rows = [{"email": "a@x.ru", "_row": 2}, {"email": "b@x.ru", "_row": 3}]
    assert check_value(t, rows) is None
    assert "max_rows = 2" in check_value(t, rows + [{"email": "c", "_row": 4}])
    assert "нет колонок: «email»" in check_value(t, [{"mail": "a", "_row": 2}])
    assert "нет строк" in check_value(t, [])
    assert "_row" in check_value(t, [{"email": "a"}])
    assert "ожидается строка" in check_value(t, [{"email": 5, "_row": 2}])


# --- чтение таблиц ----------------------------------------------------------------


def test_csv_utf8_bom_semicolon_empty_rows():
    data = "﻿email;Сумма\n\na@x.ru;100\n;\nb@x.ru;\"1;5\"\n".encode()
    assert parse_table(data, "acc.csv") == [
        {"email": "a@x.ru", "Сумма": "100", "_row": 3},
        {"email": "b@x.ru", "Сумма": "1;5", "_row": 5},
    ]


def test_csv_cp1251_and_comma():
    data = "Имя,Город\nИван,Москва\n".encode("cp1251")
    assert parse_table(data, "x.CSV") == [{"Имя": "Иван", "Город": "Москва", "_row": 2}]


@pytest.mark.parametrize("data, fragment", [
    (b"", "пустая"),
    (b"a,,b\n1,2,3\n", "пустое название"),
    (b"a,a\n1,2\n", "повторяется"),
    (b"_row,b\n1,2\n", "занята"),
    (b"a,b\n1,2,3\n", "больше, чем колонок"),
])
def test_csv_errors(data, fragment):
    with pytest.raises(TableError) as e:
        parse_table(data, "t.csv")
    assert fragment in str(e.value)


def test_header_only_and_trailing_empty_header_cells():
    assert parse_table(b"a,b,,\n", "t.csv") == []
    assert parse_table(b"a,b,\n1,2,\n", "t.csv") == [{"a": "1", "b": "2", "_row": 2}]
    with pytest.raises(TableError, match="нужна таблица .csv или .xlsx"):
        parse_table(b"x", "t.txt")


def xlsx_bytes(rows):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.create_sheet("второй").append(["не", "читается"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_first_sheet_and_cell_types(tmp_path):
    data = xlsx_bytes([
        ["email", "Сумма", "Дата", "Доля", "Флаг"],
        [None, None, None, None, None],
        ["a@x.ru", 100, dt.datetime(2026, 11, 15), 0.25, True],
        [" b@x.ru ", 7.0, dt.date(2026, 1, 2), 1.5, None],
    ])
    f = tmp_path / "acc.xlsx"
    f.write_bytes(data)
    assert read_table(f) == [
        {"email": "a@x.ru", "Сумма": "100", "Дата": "2026-11-15", "Доля": "0.25", "Флаг": "true", "_row": 3},
        {"email": "b@x.ru", "Сумма": "7", "Дата": "2026-01-02", "Доля": "1.5", "Флаг": "", "_row": 4},
    ]
    assert cell_text(dt.datetime(2026, 1, 2, 9, 30)) == "2026-01-02 09:30"


def test_xlsx_without_openpyxl_hints(monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    with pytest.raises(TableError) as e:
        parse_table(b"PK", "acc.xlsx")
    assert "openpyxl" in str(e.value) and "dependencies" in str(e.value)


def test_broken_xlsx():
    with pytest.raises(TableError, match="не читается как XLSX"):
        parse_table(b"not a zip", "acc.xlsx")


def test_missing_file(tmp_path):
    with pytest.raises(TableError, match="не найден"):
        read_table(tmp_path / "nope.csv")


# --- автономный режим и значения ---------------------------------------------------


def test_from_cli_reads_table_and_date(tmp_path):
    m = parse_manifest(manifest(TABLE))
    f = tmp_path / "acc.csv"
    f.write_text("email,Сумма\na@x.ru,1\n", encoding="utf-8")
    p = from_cli(m, ["--accounts", str(f), "--since", "2026-11-10"], {})
    assert p.accounts == [{"email": "a@x.ru", "Сумма": "1", "_row": 2}] and p.since == "2026-11-10"
    assert from_cli(m, ["--accounts", str(f)], {}).since == "2026-11-01"

    with pytest.raises(ParamsError) as e:
        from_cli(m, [], {})
    assert "--accounts файл.csv" in str(e.value)
    with pytest.raises(ParamsError, match="позже max"):
        from_cli(m, ["--accounts", str(f), "--since", "2027-01-01"], {})
    f.write_text("mail\nx\n", encoding="utf-8")
    with pytest.raises(ParamsError, match="нет колонок: «email», «Сумма»"):
        from_cli(m, ["--accounts", str(f)], {})
    with pytest.raises(ParamsError, match="не найден"):
        from_cli(m, ["--accounts", str(tmp_path / "nope.csv")], {})


def test_secret_table_masked():
    m = parse_manifest(manifest('[[params]]\nname = "logins"\ntype = "table"\ntitle = "Входы"\nsecret = true\n'))
    rows = [{"login": "ivan@mail.ru", "pass": "Pa$$w0rd", "n": "1", "_row": 2}]
    p = resolve(m, {"logins": rows}, "тест")
    assert p.to_dict() == {"logins": "***"}
    assert p.to_dict(mask_secrets=False)["logins"] == rows
    assert sorted(p.secret_values()) == ["Pa$$w0rd", "ivan@mail.ru"]  # «1» короткое, не маскируется

    out = []

    class Sink:
        def write(self, event):
            out.append(event)

    Emitter(Sink(), secrets=p.secret_values(), auto_flush=False).emit("log", msg="вход ivan@mail.ru, пароль Pa$$w0rd, строка 1")
    assert "ivan@mail.ru" not in str(out) and "Pa$$w0rd" not in str(out) and "строка 1" in str(out)
