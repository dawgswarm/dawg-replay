"""Чтение таблицы для параметра `table` (контракт 1.2) из CSV или XLSX.

Правила одни для приложения и автономного режима скрипта:

- первая строка шапка, пустые строки пропускаются, у XLSX берётся первый лист;
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
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from dawg_replay_kit.manifest import ROW_KEY

SUFFIXES = (".csv", ".xlsx")


class TableError(Exception):
    """Таблицу не прочитать. Текст готов для показа человеку."""


def read_table(path: str | Path, *, name: str | None = None) -> list[dict[str, Any]]:
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
    return parse_table(data, shown)


def parse_table(data: bytes, name: str) -> list[dict[str, Any]]:
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        return _rows(_csv_cells(data, name), name)
    if suffix == ".xlsx":
        return _rows(_xlsx_cells(data, name), name)
    raise TableError(f"{name}: нужна таблица .csv или .xlsx")


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


def _csv_cells(data: bytes, name: str) -> Iterator[list[str]]:
    text = _decode(data, name)
    try:
        yield from csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(text))
    except csv.Error as e:
        raise TableError(f"{name}: CSV не разобран: {e}") from None


def _xlsx_cells(data: bytes, name: str) -> Iterator[list[str]]:
    try:
        import openpyxl
    except ImportError:
        raise TableError(
            f"{name}: для таблиц XLSX нужен пакет openpyxl. Добавьте \"openpyxl\" в dependencies "
            "блока `# /// script` или сохраните таблицу как CSV"
        ) from None
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # openpyxl бросает разные исключения на битый файл
        raise TableError(f"{name}: файл не читается как XLSX ({type(e).__name__})") from None
    try:
        ws = wb.worksheets[0] if wb.worksheets else None
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
