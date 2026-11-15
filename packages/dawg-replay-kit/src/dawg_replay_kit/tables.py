"""Чтение таблицы для параметра `table` (контракт 1.2, 1.3) из CSV, TSV, XLSX или JSON.

Правила одни для приложения и автономного режима скрипта:

- первая строка шапка, пустые строки пропускаются; у XLSX лист из `sheet` (имя или номер
  с 1), без него первый;
- JSON это список объектов: колонки по ключам в порядке появления, `_row` номер объекта плюс 1
  (как если бы шапка была строкой 1);
- с `column_types` (контракт 1.3) значения колонок приводятся к типу (`convert_types`), ошибка
  называет строку и колонку; без него всё строки;
- значения ячеек строками: дата даёт `YYYY-MM-DD`, целое число пишется без `.0`;
- CSV в UTF-8 (с BOM или без), запасной вариант cp1251; разделитель `,` или `;`;
- строка таблицы это словарь по шапке плюс `_row`, номер строки в файле (шапка 1).

XLSX читает openpyxl. В kit его нет в зависимостях: скрипту с таблицей XLSX в
автономном режиме нужен `openpyxl` в `dependencies`, приложение ставит его само.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from dawg_replay_kit.manifest import COLUMN_TYPE_WORDS, ROW_KEY

SUFFIXES = (".csv", ".tsv", ".xlsx", ".json")
KINDS = "CSV, TSV, XLSX или JSON"


class TableError(Exception):
    """Таблицу не прочитать. Текст готов для показа человеку."""


def read_table(path: str | Path, *, name: str | None = None, sheet: str | int | None = None) -> list[dict[str, Any]]:
    """Строки таблицы из файла. `name`: имя файла для сообщений и выбора формата
    (при загрузке через форму файл лежит под временным именем)."""
    p = Path(path)
    shown = name or p.name
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        raise TableError(f"файл таблицы не найден: {p}") from None
    except OSError as e:
        raise TableError(f"не удалось прочитать {shown}: {e.strerror}") from None
    return parse_table(data, shown, sheet=sheet)


def parse_table(data: bytes, name: str, *, sheet: str | int | None = None) -> list[dict[str, Any]]:
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        return _rows(_csv_cells(data, name), name)
    if suffix == ".tsv":
        return _rows(_csv_cells(data, name, delimiter="\t"), name)
    if suffix == ".xlsx":
        return _rows(_xlsx_cells(data, name, sheet), name)
    if suffix == ".json":
        return _json_rows(data, name)
    raise TableError(f"{name}: нужна таблица {KINDS} (.csv, .tsv, .xlsx, .json)")


def sheet_names(data: bytes, name: str) -> list[str]:
    """Листы XLSX по порядку: форма предлагает выбрать, если их несколько, а `sheet` не задан."""
    wb = _open_xlsx(data, name)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


# --- ячейки --------------------------------------------------------------------


def _decode(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1251")
    except UnicodeDecodeError:
        raise TableError(f"{name}: кодировка не UTF-8 и не Windows-1251, сохраните CSV в UTF-8") from None


def _delimiter(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return ";" if line.count(";") > line.count(",") else ","
    return ","


def _csv_cells(data: bytes, name: str, delimiter: str | None = None) -> Iterator[list[str]]:
    text = _decode(data, name)
    try:
        yield from csv.reader(io.StringIO(text, newline=""), delimiter=delimiter or _delimiter(text))
    except csv.Error as e:
        raise TableError(f"{name}: CSV не разобран: {e}") from None


def _open_xlsx(data: bytes, name: str) -> Any:
    try:
        import openpyxl
    except ImportError:
        raise TableError(
            f"{name}: для таблиц XLSX нужен пакет openpyxl. Добавьте \"openpyxl\" в dependencies "
            "блока `# /// script` или сохраните таблицу как CSV"
        ) from None
    try:
        return openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # openpyxl бросает разные исключения на битый файл
        raise TableError(f"{name}: файл не читается как XLSX ({type(e).__name__})") from None


def _pick_sheet(wb: Any, name: str, sheet: str | int | None) -> Any:
    if not wb.worksheets:
        return None
    if sheet is None:
        return wb.worksheets[0]
    names = ", ".join(f"«{n}»" for n in wb.sheetnames)
    if isinstance(sheet, int) and not isinstance(sheet, bool):
        if 1 <= sheet <= len(wb.worksheets):
            return wb.worksheets[sheet - 1]
        raise TableError(f"{name}: листа номер {sheet} нет, листов {len(wb.worksheets)}: {names}")
    if sheet in wb.sheetnames:
        return wb[sheet]
    raise TableError(f"{name}: листа «{sheet}» нет, есть: {names}")


def _xlsx_cells(data: bytes, name: str, sheet: str | int | None = None) -> Iterator[list[str]]:
    wb = _open_xlsx(data, name)
    try:
        ws = _pick_sheet(wb, name, sheet)
        if ws is None:
            return
        for row in ws.iter_rows(values_only=True):
            yield [cell_text(v) for v in row]
    finally:
        wb.close()


def cell_text(value: Any) -> str:
    """Значение ячейки XLSX строкой."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.strftime("%Y-%m-%d %H:%M" if value.second == 0 else "%Y-%m-%d %H:%M:%S")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.strftime("%H:%M" if value.second == 0 else "%H:%M:%S")
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def _json_rows(data: bytes, name: str) -> list[dict[str, Any]]:
    try:
        items = json.loads(_decode(data, name))
    except json.JSONDecodeError as e:
        raise TableError(f"{name}: JSON не разобран: строка {e.lineno}, {e.msg}") from None
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise TableError(f"{name}: нужен JSON-список объектов: [{{\"колонка\": \"значение\"}}, …]")
    if not items:
        raise TableError(f"{name}: таблица пустая, в списке нет объектов")
    header: list[str] = []
    for item in items:
        for key in item:
            if key not in header:
                header.append(key)
    _header([str(k) for k in header], 1, name)
    out = []
    for i, item in enumerate(items):
        n = i + 2
        row: dict[str, Any] = {}
        for key in header:
            value = item.get(key)
            if isinstance(value, (dict, list)):
                raise TableError(f"{name}, объект {i + 1}: в колонке «{key}» вложенное значение, нужна строка или число")
            row[str(key)] = cell_text(value)
        if any(row.values()):
            row[ROW_KEY] = n
            out.append(row)
    return out


# --- типы колонок (контракт 1.3) ---------------------------------------------------

_TRUE = {"true", "1", "да", "yes", "y", "истина", "+"}
_FALSE = {"false", "0", "нет", "no", "n", "ложь", "-"}
_SPACES = re.compile(r"[\s\u00a0\u202f]")
_RU_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


def _convert(kind: str, value: Any) -> Any:
    """Значение в тип колонки. Уже приведённое возвращается как есть (повторный вызов безвреден).
    ValueError с текстом для человека, если не подходит."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None if kind != "string" else ("" if value is None else value)
    if kind == "string":
        return value if isinstance(value, str) else cell_text(value)
    text = value.strip() if isinstance(value, str) else value
    if kind == "int":
        if isinstance(text, bool):
            raise ValueError
        if isinstance(text, int):
            return text
        if isinstance(text, float) and text.is_integer():
            return int(text)
        return int(_SPACES.sub("", str(text)))
    if kind == "float":
        if isinstance(text, bool):
            raise ValueError
        if isinstance(text, (int, float)):
            number = float(text)
        else:
            number = float(_SPACES.sub("", text).replace(",", "."))
        if math.isnan(number) or math.isinf(number):
            raise ValueError
        return int(number) if isinstance(text, int) else number
    if kind == "bool":
        if isinstance(text, bool):
            return text
        low = str(text).lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError
    # date: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ, «2026-11-15 00:00» из XLSX тоже
    s = str(text)
    m = _RU_DATE.fullmatch(s)
    if m:
        s = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if len(s) > 10 and s[10] == " " and s[11:] in ("00:00", "00:00:00"):
        s = s[:10]  # полночь из ячейки даты-времени XLSX
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError
    return dt.date.fromisoformat(s).isoformat()


def convert_types(rows: list[dict[str, Any]], types: dict[str, str]) -> list[dict[str, Any]]:
    """Строки таблицы с значениями по `column_types`. Пустая ячейка типизированной колонки это None.
    TableError с номером строки и колонкой, если значение не подходит."""
    if not types:
        return rows
    out = []
    for row in rows:
        new = dict(row)
        for col, kind in types.items():
            if col not in row:
                continue
            try:
                new[col] = _convert(kind, row[col])
            except (ValueError, TypeError):
                raise TableError(
                    f"строка {row.get(ROW_KEY, '?')}, колонка «{col}»: ожидается {COLUMN_TYPE_WORDS[kind]}, "
                    f"получено «{row[col]}»"
                ) from None
        out.append(new)
    return out


# --- строки --------------------------------------------------------------------


def _rows(cells: Iterable[list[str]], name: str) -> list[dict[str, Any]]:
    header: list[str] | None = None
    out: list[dict[str, Any]] = []
    for n, raw in enumerate(cells, start=1):
        row = [c.strip() if isinstance(c, str) else cell_text(c) for c in raw]
        if not any(row):
            continue
        if header is None:
            header = _header(row, n, name)
            continue
        extra = [c for c in row[len(header):] if c]
        if extra:
            raise TableError(f"{name}, строка {n}: значений больше, чем колонок в шапке")
        item: dict[str, Any] = {col: (row[i] if i < len(row) else "") for i, col in enumerate(header)}
        item[ROW_KEY] = n
        out.append(item)
    if header is None:
        raise TableError(f"{name}: таблица пустая, нет даже шапки")
    return out


def _header(row: list[str], n: int, name: str) -> list[str]:
    while row and not row[-1]:
        row = row[:-1]
    seen: set[str] = set()
    for i, col in enumerate(row, start=1):
        if not col:
            raise TableError(f"{name}, строка {n}: у колонки {i} пустое название в шапке")
        if col == ROW_KEY:
            raise TableError(f"{name}: колонка `{ROW_KEY}` занята номером строки, переименуйте её")
        if col in seen:
            raise TableError(f"{name}: колонка «{col}» в шапке повторяется")
        seen.add(col)
    return row


def columns_of(rows: list[dict[str, Any]]) -> list[str]:
    """Колонки в порядке шапки (без `_row`)."""
    return [k for k in rows[0] if k != ROW_KEY] if rows else []
