"""Таблица по объявлению скрипта (контракт 1.3, dr-85): input, sheet, column_types, TSV и JSON."""

import io
import json

import pytest

from dawg_replay_kit.manifest import ManifestError, Param, check_value, parse_manifest
from dawg_replay_kit.tables import TableError, convert_types, parse_table, sheet_names


def manifest(params: str, contract: str = "1.3"):
    text = f'''# /// script
# dependencies = ["dawg-replay-kit"]
# ///

# /// dawg-script
# contract = "{contract}"
# name = "t"
# title = "T"
# version = 1
#
# [[params]]
# name = "rows"
# type = "table"
# title = "Строки"
{params}# ///
'''
    return parse_manifest(text)


def test_fields_parsed_and_dumped():
    m = manifest('# input = ["file", "preset"]\n# sheet = "Заказы"\n# column_types = { price = "float", qty = "int", "дата" = "date", ok = "bool" }\n')
    p = m.param("rows")
    assert p.inputs == ("file", "preset") and p.sheet == "Заказы"
    assert p.types == {"price": "float", "qty": "int", "дата": "date", "ok": "bool"}
    d = p.to_dict()
    assert d["input"] == ["file", "preset"] and d["sheet"] == "Заказы" and d["column_types"]["qty"] == "int"
    plain = manifest("").param("rows")
    assert plain.inputs == ("file", "manual", "preset") and plain.sheet is None and plain.types == {}
    assert "input" not in plain.to_dict()


@pytest.mark.parametrize("line, fragment", [
    ('# input = []\n', "непустой список"),
    ('# input = ["mail"]\n', "непустой список"),
    ('# input = ["preset"]\n', 'нужен "file" или "manual"'),
    ('# input = ["file", "file"]\n', "повторы"),
    ('# sheet = 0\n', "номер с 1"),
    ('# sheet = ""\n', "номер с 1"),
    ('# column_types = { a = "money" }\n', "тип 'money'"),
    ('# column_types = {}\n', "column_types"),
])
def test_bad_fields(line, fragment):
    with pytest.raises(ManifestError) as e:
        manifest(line)
    assert fragment in str(e.value)


def test_fields_need_contract_13():
    with pytest.raises(ManifestError) as e:
        manifest('# input = ["file"]\n# sheet = 2\n', contract="1.2")
    text = str(e.value)
    assert "поле `input` есть в контракте с версии 1.3" in text and "поле `sheet`" in text


def test_fields_only_for_table():
    text = '''# /// script
# dependencies = ["dawg-replay-kit"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "t"
# title = "T"
# version = 1
#
# [[params]]
# name = "q"
# type = "string"
# title = "Q"
# input = ["file"]
# ///
'''
    with pytest.raises(ManifestError, match="`input` бывает только у table"):
        parse_manifest(text)


# --- чтение файлов ---


def test_tsv_and_json():
    rows = parse_table("email\tcity\na@x.ru\tМосква\n\nb@x.ru\tТула\n".encode(), "a.tsv")
    assert rows == [{"email": "a@x.ru", "city": "Москва", "_row": 2}, {"email": "b@x.ru", "city": "Тула", "_row": 4}]
    data = json.dumps([{"sku": 12, "price": 99.5, "ok": True}, {"sku": "13", "note": None}, {}], ensure_ascii=False)
    rows = parse_table(data.encode(), "a.json")
    assert rows == [{"sku": "12", "price": "99.5", "ok": "true", "note": "", "_row": 2},
                    {"sku": "13", "price": "", "ok": "", "note": "", "_row": 3}]


@pytest.mark.parametrize("data, fragment", [
    (b"{}", "JSON-список объектов"),
    (b"[]", "пустая"),
    (b"[1, 2]", "JSON-список объектов"),
    (b'[{"a": [1]}]', "вложенное значение"),
    (b'[{"_row": 1}]', "занята номером строки"),
    (b"[{", "JSON не разобран"),
])
def test_json_errors(data, fragment):
    with pytest.raises(TableError, match=fragment):
        parse_table(data, "a.json")


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_sheet_by_name_and_number():
    data = xlsx({"Сводка": [["x"], ["1"]], "Заказы": [["order"], ["A-1"], ["A-2"]]})
    assert sheet_names(data, "t.xlsx") == ["Сводка", "Заказы"]
    assert parse_table(data, "t.xlsx") == [{"x": "1", "_row": 2}]  # без sheet первый лист
    assert [r["order"] for r in parse_table(data, "t.xlsx", sheet="Заказы")] == ["A-1", "A-2"]
    assert [r["order"] for r in parse_table(data, "t.xlsx", sheet=2)] == ["A-1", "A-2"]
    with pytest.raises(TableError, match="листа «Склад» нет, есть: «Сводка», «Заказы»"):
        parse_table(data, "t.xlsx", sheet="Склад")
    with pytest.raises(TableError, match="листа номер 3 нет"):
        parse_table(data, "t.xlsx", sheet=3)


# --- типы колонок ---


def test_convert_types_and_idempotent():
    rows = [{"qty": "1 000", "price": "12,5", "ok": "да", "d": "15.11.2026", "s": "x", "_row": 2},
            {"qty": "", "price": "3", "ok": "No", "d": "2026-11-01 00:00", "s": "y", "_row": 3}]
    types = {"qty": "int", "price": "float", "ok": "bool", "d": "date"}
    out = convert_types(rows, types)
    assert out[0] == {"qty": 1000, "price": 12.5, "ok": True, "d": "2026-11-15", "s": "x", "_row": 2}
    assert out[1]["qty"] is None and out[1]["price"] == 3.0 and out[1]["ok"] is False and out[1]["d"] == "2026-11-01"
    assert convert_types(out, types) == out  # повторное приведение ничего не меняет
    assert rows[0]["qty"] == "1 000"  # исходные строки не тронуты


@pytest.mark.parametrize("kind, value, word", [
    ("int", "1.5", "целое число"), ("float", "abc", "число"), ("bool", "может", "да или нет"),
    ("date", "31.02.2026", "дата ГГГГ-ММ-ДД"), ("date", "2026-11-15 10:30", "дата ГГГГ-ММ-ДД"),
])
def test_convert_errors_name_row_and_column(kind, value, word):
    with pytest.raises(TableError) as e:
        convert_types([{"c": value, "_row": 7}], {"c": kind})
    assert str(e.value) == f"строка 7, колонка «c»: ожидается {word}, получено «{value}»"


def test_check_value_accepts_typed_cells_only_in_typed_columns():
    p = Param(name="t", type="table", title="T", columns=("sku",), column_types=(("price", "float"), ("sku", "int")))
    ok = [{"sku": 0, "price": None, "name": "a", "_row": 2}]
    assert check_value(p, ok) is None  # 0 это значение, а не пустая ячейка
    assert check_value(p, [{"sku": 1, "price": 1, "name": 5, "_row": 2}]) == "строка 2, колонка «name»: ожидается строка, получено целое"
    assert "ожидается целое число" in check_value(p, [{"sku": "1", "price": 1.0, "name": "a", "_row": 2}])
    assert check_value(p, [{"sku": None, "price": 1.0, "name": "a", "_row": 2}]) == "строка 2: пустая колонка «sku»"


# --- лист XLSX из командной строки (dr-91) ---


def test_split_sheet():
    from dawg_replay_kit.tables import split_sheet

    assert split_sheet("книга.xlsx#Заказы") == ("книга.xlsx", "Заказы")
    assert split_sheet("книга.XLSX#2") == ("книга.XLSX", 2)
    assert split_sheet("a#b.csv") == ("a#b.csv", None)  # у CSV # это часть имени
    assert split_sheet("книга.xlsx") == ("книга.xlsx", None)
    assert split_sheet("книга.xlsx#") == ("книга.xlsx#", None)


def test_read_table_arg_sheet_and_empty_first_sheet(tmp_path):
    from dawg_replay_kit.tables import read_table_arg

    f = tmp_path / "книга.xlsx"
    f.write_bytes(xlsx({"Сводка": [["Итого", "10"]], "Заказы": [["order"], ["A-1"], ["A-2"]]}))
    assert [r["order"] for r in read_table_arg(f"{f}#Заказы")] == ["A-1", "A-2"]
    assert [r["order"] for r in read_table_arg(f"{f}#2")] == ["A-1", "A-2"]
    with pytest.raises(TableError) as e:
        read_table_arg(str(f))
    assert str(e.value) == "книга.xlsx: на листе «Сводка» (первом) нет строк. Листы: «Сводка», «Заказы»; выберите нужный: книга.xlsx#Заказы"
    with pytest.raises(TableError, match="лист задан скриптом"):
        read_table_arg(f"{f}#Заказы", declared="Заказы")
    assert [r["order"] for r in read_table_arg(str(f), declared="Заказы")] == ["A-1", "A-2"]
