"""Статическое чтение и проверка манифеста скрипта (docs/contract.md, разделы 1 и 2).

Скрипт не запускается и его зависимости не ставятся: читаются только блоки
комментариев PEP 723 в начале файла.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dawg_replay_kit import relative

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

SUPPORTED_CONTRACTS: tuple[int, ...] = (1,)
"""Мажорные версии контракта, которые понимает эта версия kit."""

CONTRACT_MINORS: dict[int, int] = {1: 5}
"""Последняя минорная версия каждого мажора, которую знает эта версия kit."""


def contract_versions() -> list[str]:
    """Полные версии для `dawg-replay --version`: ["1.5"]."""
    return [f"{major}.{CONTRACT_MINORS[major]}" for major in SUPPORTED_CONTRACTS]


MANIFEST_BLOCK = "dawg-script"
SCRIPT_BLOCK = "script"

DEFAULT_TIMEOUT_MINUTES = 60

PARAM_TYPES: tuple[str, ...] = (
    "string",
    "text",
    "int",
    "float",
    "bool",
    "enum",
    "url",
    "url_list",
    "string_list",
    "secret",
    "date",
    "table",
    "time",
    "datetime",
    "multi_enum",
    "file",
)
NUMBER_TYPES = ("int", "float")
LIST_TYPES = ("url_list", "string_list")

# Имена, занятые аргументами автономного режима.
RESERVED_PARAM_NAMES = ("params", "help")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLUG_MAX = 64
_PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CONTRACT_STR_RE = re.compile(r"^(\d+)\.(\d+)$")

# Регулярное выражение из спецификации PEP 723 (inline script metadata).
_BLOCK_RE = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$"
)
_BLOCK_OPEN_RE = re.compile(r"^# /// (?P<type>[a-zA-Z0-9-]+)\s*$")
_TOML_LINE_RE = re.compile(r"line (\d+)")

_TOP_KEYS = (
    "contract", "name", "title", "description", "site", "tags", "version", "timeout_minutes", "params", "outputs",
)
# Поля, появившиеся в минорных версиях: поле -> (мажор, минор).
_FIELD_SINCE: dict[str, tuple[int, int]] = {"site": (1, 1), "tags": (1, 1), "outputs": (1, 4)}
# Типы параметров, появившиеся в минорных версиях.
_TYPE_SINCE: dict[str, tuple[int, int]] = {
    "date": (1, 2), "table": (1, 2), "time": (1, 3), "datetime": (1, 3), "multi_enum": (1, 3), "file": (1, 3),
}
# Поля параметров, появившиеся в минорных версиях.
_PARAM_FIELD_SINCE: dict[str, tuple[int, int]] = {"input": (1, 3), "sheet": (1, 3), "column_types": (1, 3)}
# table: как человек задаёт таблицу; по умолчанию все способы.
TABLE_INPUTS = ("file", "manual", "preset")
# table: типы колонок; без column_types все значения строки.
COLUMN_TYPES = ("string", "int", "float", "bool", "date")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d$")
CHOICE_TYPES = ("enum", "multi_enum")
MOMENT_TYPES = ("date", "time", "datetime")  # min/max строками того же вида
DEFAULT_MAX_ROWS = 10000
DEFAULT_MAX_MB = 25  # file: предел размера одного файла по умолчанию
MAX_MB_LIMIT = 1024
FILE_KEYS = ("path", "name", "size")  # значение file: {"path", "name", "size"} или их список
ROW_KEY = "_row"

OUTPUTS_MAX = 50  # outputs (1.4): сколько файлов можно объявить
OUTPUT_NAME_MAX = 200
OUTPUT_TITLE_MAX = 80
_OUTPUT_KEYS = ("name", "title", "columns", "main", "optional", "kind")
# outputs (1.5): выходное значение `kind = "value"`, его поля и типы
OUTPUT_KINDS = ("file", "value")
_VALUE_OUTPUT_KEYS = ("name", "title", "kind", "type", "optional", "secret", "choices")
VALUE_TYPES: tuple[str, ...] = (
    "string", "text", "int", "float", "bool", "enum", "multi_enum", "url", "url_list", "string_list",
    "date", "time", "datetime",
)
_GLOB_CHARS = frozenset("*?[")

SITE_MAX = 40
TAGS_MAX = 5
TAG_MAX = 24
_PARAM_KEYS = (
    "name", "type", "title", "description", "required", "default", "min", "max", "min_items", "max_items",
    "choices", "columns", "max_rows", "secret", "input", "sheet", "column_types", "accept", "max_mb", "multiple",
)


@dataclass(frozen=True)
class Issue:
    """Одна ошибка манифеста.

    code: стабильный код для машин; path: где ошибка (`params[1].default`);
    line: строка файла, если известна.
    """

    code: str
    path: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "message": self.message, "line": self.line}

    def __str__(self) -> str:
        where = self.path
        if self.line is not None:
            where = f"строка {self.line}" + (f", {self.path}" if self.path else "")
        return f"{where}: {self.message}" if where else self.message


class ManifestError(Exception):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("\n".join(str(i) for i in issues))


@dataclass(frozen=True)
class Param:
    name: str
    type: str
    title: str
    description: str | None = None
    required: bool = False
    default: Any = None
    min: int | float | None = None
    max: int | float | None = None
    max_items: int | None = None
    choices: tuple[str, ...] | None = None  # значения; подписи отдельно в choice_titles
    columns: tuple[str, ...] | None = None  # table: колонки, которые обязаны быть в шапке
    max_rows: int | None = None  # table: предел строк, без поля DEFAULT_MAX_ROWS
    secret: bool = False  # table: таблица целиком секрет; file: файл секретный (ключ, сертификат)
    input: tuple[str, ...] | None = None  # table: способы ввода из TABLE_INPUTS, None = все
    sheet: str | int | None = None  # table: лист XLSX по имени или номеру с 1, None = первый
    column_types: tuple[tuple[str, str], ...] | None = None  # table: (колонка, тип) из COLUMN_TYPES
    choice_titles: tuple[tuple[str, str], ...] | None = None  # enum, multi_enum (1.3): (value, title)
    min_items: int | None = None  # multi_enum (1.3)
    accept: tuple[str, ...] | None = None  # file (1.3): расширения (".pdf") или mime ("image/*")
    max_mb: float | None = None  # file (1.3): предел размера одного файла, без поля DEFAULT_MAX_MB
    multiple: bool = False  # file (1.3): несколько файлов, значение список

    def choice_title(self, value: str) -> str:
        """Подпись варианта для людей: title из манифеста или само значение."""
        return dict(self.choice_titles or ()).get(value, value)

    @property
    def has_default(self) -> bool:
        return self.default is not None

    @property
    def is_secret(self) -> bool:
        """Значение не хранится и не показывается: `secret` или `table` с `secret = true`."""
        return self.type == "secret" or (self.type in ("table", "file") and self.secret)

    @property
    def size_limit(self) -> int:
        """file: предел размера одного файла в байтах."""
        return int((self.max_mb if self.max_mb is not None else DEFAULT_MAX_MB) * 1024 * 1024)

    @property
    def inputs(self) -> tuple[str, ...]:
        """Способы ввода таблицы, которые объявил скрипт (без поля все)."""
        return self.input if self.input is not None else TABLE_INPUTS

    @property
    def types(self) -> dict[str, str]:
        """Типы колонок таблицы; колонки без типа строки."""
        return dict(self.column_types) if self.column_types else {}

    @property
    def row_limit(self) -> int:
        return self.max_rows if self.max_rows is not None else DEFAULT_MAX_ROWS

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type, "title": self.title, "required": self.required}
        for key in ("description", "default", "min", "max", "min_items", "max_items", "max_rows"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        if self.choices is not None and self.choice_titles:
            d["choices"] = [{"value": v, "title": t} for v, t in self.choice_titles]
        elif self.choices is not None:
            d["choices"] = list(self.choices)
        if self.columns is not None:
            d["columns"] = list(self.columns)
        if self.secret:
            d["secret"] = True
        if self.accept is not None:
            d["accept"] = list(self.accept)
        if self.max_mb is not None:
            d["max_mb"] = self.max_mb
        if self.multiple:
            d["multiple"] = True
        if self.input is not None:
            d["input"] = list(self.input)
        if self.sheet is not None:
            d["sheet"] = self.sheet
        if self.column_types is not None:
            d["column_types"] = dict(self.column_types)
        return d


@dataclass(frozen=True)
class Output:
    """Результат, который скрипт объявил в манифесте (блок `[[outputs]]`).

    Файл (контракт 1.4, `kind = "file"`): name это путь внутри каталога результата (output dir)
    через `/`, можно шаблон `prices-*.csv`.
    Значение (контракт 1.5, `kind = "value"`): name как у параметра, `type` из VALUE_TYPES,
    скрипт отдаёт его вызовом `kit.output(name, value)`.
    """

    name: str
    title: str | None = None
    columns: tuple[str, ...] | None = None  # для таблиц: колонки, которые в файле будут
    main: bool = False  # главный файл итога
    optional: bool = False  # бывает не в каждом запуске (ветвление): его отсутствие не поломка
    kind: str = "file"
    type: str | None = None  # значение: тип из VALUE_TYPES
    secret: bool = False  # значение: не показывается и не пишется в журнал
    choices: tuple[str, ...] | None = None  # значение enum и multi_enum: допустимые варианты

    @property
    def is_value(self) -> bool:
        return self.kind == "value"

    def as_param(self) -> Param:
        """Значение как параметр того же типа: для проверки check_value."""
        return Param(name=self.name, type=self.type or "string", title=self.title or self.name, choices=self.choices)

    def check(self, value: Any) -> str | None:
        """Проверка отданного значения. Относительные даты («today») не годятся: нужна сама дата."""
        if self.type in ("date", "datetime") and relative.is_relative(value, self.type):
            return f"нужна дата, а не выражение {value!r}"
        return check_value(self.as_param(), value)

    @property
    def label(self) -> str:
        return self.title or self.name

    @property
    def is_pattern(self) -> bool:
        return any(c in _GLOB_CHARS for c in self.name)

    def matches(self, path: str) -> bool:
        """Подходит ли файл (путь внутри output dir через `/`). `*` не переходит в другой каталог."""
        if not self.is_pattern:
            return path == self.name
        if path.count("/") != self.name.count("/"):
            return False
        return fnmatch.fnmatchcase(path, self.name)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.is_value:
            d["kind"] = "value"
            d["type"] = self.type
            if self.title is not None:
                d["title"] = self.title
            if self.optional:
                d["optional"] = True
            if self.secret:
                d["secret"] = True
            if self.choices is not None:
                d["choices"] = list(self.choices)
            return d
        if self.title is not None:
            d["title"] = self.title
        if self.columns is not None:
            d["columns"] = list(self.columns)
        if self.main:
            d["main"] = True
        if self.optional:
            d["optional"] = True
        return d


@dataclass(frozen=True)
class Manifest:
    contract: int
    contract_minor: int
    name: str
    title: str
    version: int
    description: str | None = None
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES
    params: tuple[Param, ...] = ()
    requires_python: str | None = None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    site: str | None = None  # контракт 1.1: название сайта для людей
    tags: tuple[str, ...] = ()  # контракт 1.1: метки для библиотеки скриптов
    outputs: tuple[Output, ...] = ()  # контракт 1.4: объявленные файлы результата, 1.5: и значения

    def files_out(self) -> tuple[Output, ...]:
        """Объявленные файлы результата."""
        return tuple(o for o in self.outputs if not o.is_value)

    def values_out(self) -> tuple[Output, ...]:
        """Объявленные выходные значения (контракт 1.5)."""
        return tuple(o for o in self.outputs if o.is_value)

    def value_out(self, name: str) -> Output:
        for o in self.outputs:
            if o.is_value and o.name == name:
                return o
        raise KeyError(name)

    @property
    def main_output(self) -> Output | None:
        return next((o for o in self.outputs if o.main), None)

    def param(self, name: str) -> Param:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "contract": self.contract,
            "contract_minor": self.contract_minor,
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "timeout_minutes": self.timeout_minutes,
            "params": [p.to_dict() for p in self.params],
            "requires_python": self.requires_python,
            "dependencies": list(self.dependencies),
        }
        if self.description is not None:
            d["description"] = self.description
        if self.site is not None:
            d["site"] = self.site
        if self.tags:
            d["tags"] = list(self.tags)
        if self.outputs:
            d["outputs"] = [o.to_dict() for o in self.outputs]
        return d


# --- чтение блоков ---------------------------------------------------------


@dataclass(frozen=True)
class _Block:
    content: str
    first_line: int  # номер строки файла, с которой начинается содержимое


def _read_blocks(text: str, issues: list[Issue]) -> dict[str, _Block]:
    blocks: dict[str, _Block] = {}
    covered: set[int] = set()
    for m in _BLOCK_RE.finditer(text):
        kind = m.group("type")
        open_line = text.count("\n", 0, m.start()) + 1
        covered.add(open_line)
        if kind in blocks:
            issues.append(Issue("duplicate_block", "", f"блок `{kind}` встречается дважды", open_line))
            continue
        lines = [
            line[2:] if line.startswith("# ") else line[1:]
            for line in m.group("content").splitlines(keepends=True)
        ]
        blocks[kind] = _Block("".join(lines), open_line + 1)

    for n, line in enumerate(text.splitlines(), start=1):
        om = _BLOCK_OPEN_RE.match(line)
        if om and n not in covered and om.group("type") in (MANIFEST_BLOCK, SCRIPT_BLOCK):
            issues.append(
                Issue(
                    "unclosed_block",
                    "",
                    f"блок `{om.group('type')}` не закрыт строкой `# ///` "
                    "или в нём есть строка, которая не начинается с `# `",
                    n,
                )
            )
    return blocks


def _parse_toml(block: _Block, kind: str, issues: list[Issue]) -> dict[str, Any] | None:
    try:
        return tomllib.loads(block.content)
    except tomllib.TOMLDecodeError as e:
        msg = str(e)
        line = None
        lm = _TOML_LINE_RE.search(msg)
        if lm:
            line = block.first_line + int(lm.group(1)) - 1
            msg = _TOML_LINE_RE.sub(f"line {line}", msg)
        issues.append(Issue("toml_syntax", "", f"блок `{kind}`: ошибка TOML: {msg}", line))
        return None


# --- проверка значений -----------------------------------------------------


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_url(v: Any) -> bool:
    if not isinstance(v, str) or not v or any(c.isspace() for c in v):
        return False
    u = urlparse(v)
    return u.scheme in ("http", "https") and bool(u.netloc)


def _type_name(v: Any) -> str:
    return {
        bool: "bool",
        int: "целое",
        float: "дробное",
        str: "строка",
        list: "список",
        dict: "объект",
    }.get(type(v), type(v).__name__)


def check_value(param: Param, value: Any) -> str | None:
    """Проверяет значение параметра. Возвращает текст ошибки или None.

    Используется и для `default`, и для значений, которые приходят при запуске.
    """
    t = param.type
    if t in ("string", "text", "secret"):
        if not isinstance(value, str):
            return f"ожидается строка, получено {_type_name(value)}"
        if t == "string" and "\n" in value:
            return "в типе `string` нельзя переводить строку, для многострочного текста есть `text`"
        return None
    if t == "bool":
        return None if isinstance(value, bool) else f"ожидается true или false, получено {_type_name(value)}"
    if t in NUMBER_TYPES:
        ok = _is_int(value) if t == "int" else _is_number(value)
        if not ok:
            return f"ожидается {'целое' if t == 'int' else 'число'}, получено {_type_name(value)}"
        if param.min is not None and value < param.min:
            return f"значение {value} меньше min = {param.min}"
        if param.max is not None and value > param.max:
            return f"значение {value} больше max = {param.max}"
        return None
    if t == "enum":
        if not isinstance(value, str):
            return f"ожидается строка из choices, получено {_type_name(value)}"
        if param.choices is not None and value not in param.choices:
            return f"`{value}` нет в choices: {_choices_text(param)}"
        return None
    if t == "multi_enum":
        if not isinstance(value, list):
            return f"ожидается список значений из choices, получено {_type_name(value)}"
        for item in value:
            if not isinstance(item, str):
                return f"ожидается строка из choices, получено {_type_name(item)}"
            if param.choices is not None and item not in param.choices:
                return f"`{item}` нет в choices: {_choices_text(param)}"
        if len(set(value)) != len(value):
            return "значения повторяются"
        if param.min_items is not None and len(value) < param.min_items:
            return f"выбрано {len(value)}, а min_items = {param.min_items}"
        if param.max_items is not None and len(value) > param.max_items:
            return f"выбрано {len(value)}, а max_items = {param.max_items}"
        return None
    if t in ("time", "datetime"):
        if t == "datetime" and relative.is_relative(value, "datetime"):
            return None  # выражение: дата появится при запуске, тогда и проверка min/max
        problem = _moment_problem(t, value)
        if problem:
            return problem
        if param.min is not None and value < param.min:
            return f"{value} раньше min = {param.min}"
        if param.max is not None and value > param.max:
            return f"{value} позже max = {param.max}"
        return None
    if t == "url":
        return None if _is_url(value) else f"ожидается адрес http(s)://..., получено {value!r}"
    if t == "date":
        if relative.is_relative(value, "date"):
            return None  # выражение: дата появится при запуске, тогда и проверка min/max
        problem = _date_problem(value)
        if problem:
            return problem
        if param.min is not None and value < param.min:
            return f"дата {value} раньше min = {param.min}"
        if param.max is not None and value > param.max:
            return f"дата {value} позже max = {param.max}"
        return None
    if t == "table":
        return _table_problem(param, value)
    if t == "file":
        return _file_problem(param, value)
    if t in LIST_TYPES:
        if not isinstance(value, list):
            return f"ожидается список, получено {_type_name(value)}"
        for i, item in enumerate(value):
            if t == "url_list" and not _is_url(item):
                return f"элемент {i}: ожидается адрес http(s)://..., получено {item!r}"
            if t == "string_list" and not isinstance(item, str):
                return f"элемент {i}: ожидается строка, получено {_type_name(item)}"
        if param.max_items is not None and len(value) > param.max_items:
            return f"элементов {len(value)}, а max_items = {param.max_items}"
        return None
    return f"неизвестный тип `{t}`"


def _choices_text(param: Param) -> str:
    """Варианты для ошибки: `price (Сначала дешевле), rating`; подпись только если она есть."""
    return ", ".join(
        f"{v} ({param.choice_title(v)})" if param.choice_title(v) != v else v for v in param.choices or ()
    )


def _moment_problem(t: str, value: Any) -> str | None:
    """Проверка вида для date, time и datetime (без min/max)."""
    if t == "date":
        return _date_problem(value)
    if t == "time":
        if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
            return f"ожидается время ЧЧ:ММ, получено {value!r}"
        return None
    if not isinstance(value, str) or not _DATETIME_RE.fullmatch(value):
        return f"ожидается дата и время ГГГГ-ММ-ДДTЧЧ:ММ, получено {value!r}"
    return _date_problem(value[:10])


def _date_problem(value: Any) -> str | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return f"ожидается дата ГГГГ-ММ-ДД, получено {value!r}"
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return f"такой даты нет: {value}"
    return None


def accepts(param: Param, name: str) -> bool:
    """Подходит ли файл по `accept`: расширение (".pdf") или mime по имени ("image/*", "application/pdf")."""
    if not param.accept:
        return True
    import mimetypes

    low = name.lower()
    mime = mimetypes.guess_type(low)[0] or ""
    for a in param.accept:
        a = a.lower()
        if a.startswith("."):
            if low.endswith(a):
                return True
        elif a.endswith("/*"):
            if mime.startswith(a[:-1]):
                return True
        elif mime == a:
            return True
    return False


def _file_problem(param: Param, value: Any) -> str | None:
    """Файл: {"path", "name", "size"}; при `multiple` список таких."""
    items = value if param.multiple else [value]
    if param.multiple:
        if not isinstance(value, list):
            return f"ожидается список файлов, получено {_type_name(value)}"
        if not value:
            return "файлов нет"
        if param.max_items is not None and len(value) > param.max_items:
            return f"файлов {len(value)}, а max_items = {param.max_items}"
    for i, item in enumerate(items):
        where = f"файл {i + 1}: " if param.multiple else ""
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("name"), str):
            return f"{where}ожидается объект {{path, name, size}}, получено {_type_name(item)}"
        size = item.get("size")
        if not _is_int(size) or size < 0:
            return f"{where}нет размера файла"
        if not accepts(param, item["name"]):
            return f"{where}«{item['name']}» не подходит, нужен: {', '.join(param.accept or ())}"
        if size > param.size_limit:
            return f"{where}«{item['name']}» больше {_mb(param.size_limit)} МБ"
    return None


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:g}".replace(".", ",")


def _cell_empty(cell: Any) -> bool:
    return cell is None or (isinstance(cell, str) and not cell.strip())


def _cell_problem(kind: str, cell: Any) -> str | None:
    """Значение ячейки по типу колонки (column_types). Пустая типизированная ячейка это null."""
    if kind == "string":
        return None if isinstance(cell, str) else f"ожидается строка, получено {_type_name(cell)}"
    if cell is None:
        return None
    if kind == "int":
        ok = _is_int(cell)
    elif kind == "float":
        ok = isinstance(cell, (int, float)) and not isinstance(cell, bool)
    elif kind == "bool":
        ok = isinstance(cell, bool)
    else:  # date
        ok = isinstance(cell, str) and _DATE_RE.fullmatch(cell) is not None
    return None if ok else f"ожидается {COLUMN_TYPE_WORDS[kind]}, получено {cell!r}"


COLUMN_TYPE_WORDS = {"string": "строка", "int": "целое число", "float": "число", "bool": "да или нет",
                     "date": "дата ГГГГ-ММ-ДД"}


def _table_problem(param: Param, value: Any) -> str | None:
    """Таблица: список строк-объектов со строковыми значениями и номером `_row`."""
    if not isinstance(value, list):
        return f"ожидается таблица (список строк), получено {_type_name(value)}"
    if not value:
        return "в таблице нет строк"
    if len(value) > param.row_limit:
        return f"строк {len(value)}, а max_rows = {param.row_limit}"
    types = param.types
    for i, row in enumerate(value):
        if not isinstance(row, dict):
            return f"строка {i}: ожидается объект, получено {_type_name(row)}"
        n = row.get(ROW_KEY)
        if not _is_int(n) or n < 2:
            return f"строка {i}: нет номера `{ROW_KEY}` (целое, шапка это строка 1)"
        for key, cell in row.items():
            if key == ROW_KEY:
                continue
            problem = _cell_problem(types.get(key, "string"), cell)
            if problem:
                return f"строка {n}, колонка «{key}»: {problem}"
    if param.columns:
        missing = [c for c in param.columns if c not in value[0]]
        if missing:
            return "в шапке нет колонок: " + ", ".join(f"«{c}»" for c in missing)
        # Колонки из `columns` нужны скрипту в каждой строке: строка с паролем без логина
        # упала бы потом как неверный вход. Одно правило для файла, ввода вручную и --row.
        for row in value:
            empty = [c for c in param.columns if _cell_empty(row.get(c))]
            if empty:
                return f"строка {row[ROW_KEY]}: " + (
                    f"пустая колонка «{empty[0]}»" if len(empty) == 1
                    else "пустые колонки " + ", ".join(f"«{c}»" for c in empty))
    return None


def _check_param(raw: Any, idx: int, seen: set[str], issues: list[Issue]) -> Param | None:
    base = f"params[{idx}]"
    if not isinstance(raw, dict):
        issues.append(Issue("param_not_table", base, "параметр задаётся таблицей `[[params]]`"))
        return None

    n_before = len(issues)

    def err(code: str, key: str, msg: str) -> None:
        issues.append(Issue(code, f"{base}.{key}" if key else base, msg))

    name = raw.get("name")
    if name is None:
        err("missing_field", "name", "нет обязательного поля `name`")
    elif not isinstance(name, str) or not _PARAM_NAME_RE.fullmatch(name):
        err("bad_param_name", "name", f"имя {name!r} не подходит: нужны строчные латинские буквы, цифры и `_`, первая буква")
    elif name in RESERVED_PARAM_NAMES:
        err("reserved_param_name", "name", f"имя `{name}` занято аргументом автономного режима")
    elif name in seen:
        err("duplicate_param", "name", f"параметр `{name}` уже объявлен")
    else:
        seen.add(name)
        base = f"params[{idx}]({name})"

    for key in raw:
        if key not in _PARAM_KEYS:
            err("unknown_field", key, f"неизвестное поле `{key}`; допустимы: {', '.join(_PARAM_KEYS)}")

    t = raw.get("type")
    if t is None:
        err("missing_field", "type", "нет обязательного поля `type`")
    elif t not in PARAM_TYPES:
        err("bad_type", "type", f"неизвестный тип {t!r}; допустимы: {', '.join(PARAM_TYPES)}")
        t = None

    title = raw.get("title")
    if title is None:
        err("missing_field", "title", "нет обязательного поля `title`")
    elif not isinstance(title, str) or not title.strip():
        err("bad_value", "title", "`title` должен быть непустой строкой")

    desc = raw.get("description")
    if desc is not None and not isinstance(desc, str):
        err("bad_value", "description", "`description` должен быть строкой")

    required = raw.get("required", False)
    if not isinstance(required, bool):
        err("bad_value", "required", "`required` должен быть true или false")

    lo, hi = raw.get("min"), raw.get("max")
    for key, v in (("min", lo), ("max", hi)):
        if v is None:
            continue
        if t is not None and t not in (*NUMBER_TYPES, *MOMENT_TYPES):
            err("field_not_allowed", key, f"`{key}` бывает только у int, float, date, time и datetime")
        elif t in MOMENT_TYPES:
            problem = _moment_problem(t, v)
            if problem:
                err("bad_value", key, f"`{key}` у {t}: {problem}")
        elif t == "int" and not _is_int(v):
            err("bad_value", key, f"`{key}` у int должен быть целым")
        elif not _is_number(v):
            err("bad_value", key, f"`{key}` должен быть числом")
    if t in MOMENT_TYPES:
        if isinstance(lo, str) and isinstance(hi, str) and not _moment_problem(t, lo) and not _moment_problem(t, hi) and lo > hi:
            err("bad_range", "min", f"min = {lo} позже max = {hi}")
    elif _is_number(lo) and _is_number(hi) and lo > hi:
        err("bad_range", "min", f"min = {lo} больше max = {hi}")

    columns = raw.get("columns")
    if columns is not None:
        if t is not None and t != "table":
            err("field_not_allowed", "columns", "`columns` бывает только у table")
        elif (
            not isinstance(columns, list)
            or not all(isinstance(c, str) and c.strip() and c == c.strip() for c in columns)
        ):
            err("bad_value", "columns", "`columns` должен быть списком непустых названий колонок")
        elif len(set(columns)) != len(columns):
            err("bad_value", "columns", "в `columns` есть повторы")
        elif ROW_KEY in columns:
            err("bad_value", "columns", f"`{ROW_KEY}` не колонка, это номер строки")

    max_rows = raw.get("max_rows")
    if max_rows is not None:
        if t is not None and t != "table":
            err("field_not_allowed", "max_rows", "`max_rows` бывает только у table")
        elif not _is_int(max_rows) or max_rows < 1:
            err("bad_value", "max_rows", "`max_rows` должен быть целым не меньше 1")

    secret = raw.get("secret", False)
    if "secret" in raw:
        if t is not None and t not in ("table", "file"):
            err("field_not_allowed", "secret", "`secret` бывает только у table и file; для секретной строки есть тип secret")
        elif not isinstance(secret, bool):
            err("bad_value", "secret", "`secret` должен быть true или false")

    accept = raw.get("accept")
    if accept is not None:
        if t is not None and t != "file":
            err("field_not_allowed", "accept", "`accept` бывает только у file")
        elif (not isinstance(accept, list) or not accept
              or not all(isinstance(a, str) and (a.startswith(".") and len(a) > 1 or "/" in a) for a in accept)):
            err("bad_value", "accept", "`accept` это список расширений (\".pdf\") или типов (\"image/*\")")

    max_mb = raw.get("max_mb")
    if max_mb is not None:
        if t is not None and t != "file":
            err("field_not_allowed", "max_mb", "`max_mb` бывает только у file")
        elif not _is_number(max_mb) or not 0 < max_mb <= MAX_MB_LIMIT:
            err("bad_value", "max_mb", f"`max_mb` число больше 0 и не больше {MAX_MB_LIMIT}")

    multiple = raw.get("multiple", False)
    if "multiple" in raw:
        if t is not None and t != "file":
            err("field_not_allowed", "multiple", "`multiple` бывает только у file")
        elif not isinstance(multiple, bool):
            err("bad_value", "multiple", "`multiple` должен быть true или false")

    max_items = raw.get("max_items")
    if max_items is not None:
        if t == "file" and not multiple:
            err("field_not_allowed", "max_items", "`max_items` у file бывает только с `multiple = true`")
        elif t is not None and t not in (*LIST_TYPES, "multi_enum", "file"):
            err("field_not_allowed", "max_items", "`max_items` бывает только у url_list, string_list, multi_enum и file")
        elif not _is_int(max_items) or max_items < 1:
            err("bad_value", "max_items", "`max_items` должен быть целым не меньше 1")

    min_items = raw.get("min_items")
    if min_items is not None:
        if t is not None and t != "multi_enum":
            err("field_not_allowed", "min_items", "`min_items` бывает только у multi_enum")
        elif not _is_int(min_items) or min_items < 1:
            err("bad_value", "min_items", "`min_items` должен быть целым не меньше 1")
        elif _is_int(max_items) and min_items > max_items:
            err("bad_range", "min_items", f"min_items = {min_items} больше max_items = {max_items}")

    choices = raw.get("choices")
    values: list[str] = []
    titles: list[tuple[str, str]] = []
    if t in CHOICE_TYPES:
        problem = None
        if choices is None:
            err("missing_field", "choices", f"у {t} обязателен список `choices`")
        elif not isinstance(choices, list) or not choices:
            problem = "`choices` должен быть непустым списком"
        else:
            for c in choices:
                if isinstance(c, str) and c:
                    values.append(c)
                    titles.append((c, c))
                elif (
                    isinstance(c, dict) and set(c) <= {"value", "title"}
                    and isinstance(c.get("value"), str) and c["value"]
                    and isinstance(c.get("title"), str) and c["title"].strip()
                ):
                    values.append(c["value"])
                    titles.append((c["value"], c["title"]))
                else:
                    problem = ("вариант в `choices`: непустая строка или таблица "
                               "{ value = \"...\", title = \"...\" } с непустыми value и title")
                    break
        if problem:
            err("bad_value", "choices", problem)
        elif choices is not None and len(set(values)) != len(values):
            err("bad_value", "choices", "в `choices` есть повторы")
    elif choices is not None and t is not None:
        err("field_not_allowed", "choices", "`choices` бывает только у enum и multi_enum")

    default = raw.get("default")
    table_input = raw.get("input")
    if table_input is not None:
        if t is not None and t != "table":
            err("field_not_allowed", "input", "`input` бывает только у table")
        elif (
            not isinstance(table_input, list) or not table_input
            or not all(isinstance(x, str) and x in TABLE_INPUTS for x in table_input)
        ):
            err("bad_value", "input", "`input` это непустой список из " + ", ".join(f'"{x}"' for x in TABLE_INPUTS))
        elif len(set(table_input)) != len(table_input):
            err("bad_value", "input", "в `input` есть повторы")
        elif "file" not in table_input and "manual" not in table_input:
            err("bad_value", "input", 'в `input` нужен "file" или "manual": таблицу из пресета сначала откуда-то берут')

    sheet = raw.get("sheet")
    if sheet is not None:
        if t is not None and t != "table":
            err("field_not_allowed", "sheet", "`sheet` бывает только у table")
        elif isinstance(sheet, bool) or not (
            (isinstance(sheet, str) and sheet.strip()) or (_is_int(sheet) and sheet >= 1)
        ):
            err("bad_value", "sheet", "`sheet` это имя листа XLSX или его номер с 1")

    column_types = raw.get("column_types")
    if column_types is not None:
        if t is not None and t != "table":
            err("field_not_allowed", "column_types", "`column_types` бывает только у table")
        elif not isinstance(column_types, dict) or not column_types:
            err("bad_value", "column_types", '`column_types` это таблица { колонка = "тип" }')
        else:
            for col, ct in column_types.items():
                if not isinstance(col, str) or not col.strip() or col == ROW_KEY:
                    err("bad_value", "column_types", f"имя колонки {col!r} не подходит")
                elif ct not in COLUMN_TYPES:
                    err("bad_value", "column_types", f"колонка «{col}»: тип {ct!r}, допустимы: {', '.join(COLUMN_TYPES)}")

    if default is not None and t == "secret":
        err("field_not_allowed", "default", "у secret не бывает `default`: секрет не хранится в скрипте")
        default = None
    elif default is not None and t == "table":
        err("field_not_allowed", "default", "у table не бывает `default`: таблица приходит из файла")
        default = None
    elif default is not None and t == "file":
        err("field_not_allowed", "default", "у file не бывает `default`: файл даёт человек при запуске")
        default = None

    if len(issues) > n_before:
        return None

    param = Param(
        name=name,
        type=t,
        title=title,
        description=desc,
        required=required,
        default=None,
        min=lo,
        max=hi,
        max_items=max_items,
        choices=tuple(values) if choices is not None else None,
        choice_titles=tuple(titles) if any(isinstance(c, dict) for c in choices or ()) else None,
        min_items=min_items,
        columns=tuple(columns) if columns is not None else None,
        max_rows=max_rows,
        secret=bool(secret),
        input=tuple(table_input) if table_input is not None else None,
        sheet=sheet,
        column_types=tuple((str(c), str(v)) for c, v in column_types.items()) if column_types is not None else None,
        accept=tuple(accept) if accept is not None else None,
        max_mb=max_mb,
        multiple=bool(multiple),
    )
    if default is not None:
        problem = check_value(param, default)
        if problem:
            err("bad_default", "default", problem)
            return None
        param = replace(param, default=default)
    return param


def _check_manifest(data: dict[str, Any], issues: list[Issue]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    unknown = [key for key in data if key not in _TOP_KEYS]

    contract = data.get("contract")
    if contract is None:
        issues.append(Issue("missing_field", "contract", "нет обязательного поля `contract`"))
    else:
        major = minor = None
        if _is_int(contract):
            major, minor = contract, 0
        elif isinstance(contract, str) and _CONTRACT_STR_RE.fullmatch(contract):
            cm = _CONTRACT_STR_RE.fullmatch(contract)
            major, minor = int(cm.group(1)), int(cm.group(2))
        if major is None:
            issues.append(Issue("bad_value", "contract", f"`contract` задаётся целым (1) или строкой \"1.2\", получено {contract!r}"))
        elif major not in SUPPORTED_CONTRACTS:
            supported = ", ".join(str(v) for v in SUPPORTED_CONTRACTS)
            issues.append(
                Issue(
                    "unsupported_contract",
                    "contract",
                    f"скрипт написан под контракт {major}, эта версия поддерживает: {supported}. "
                    "Обновите dawg-replay или перепишите скрипт под поддерживаемый контракт",
                )
            )
        else:
            out["contract"], out["contract_minor"] = major, minor

    for key in unknown:
        msg = f"неизвестное поле `{key}`; допустимы: {', '.join(_TOP_KEYS)}"
        known = CONTRACT_MINORS.get(out.get("contract", -1))
        if known is not None and out["contract_minor"] > known:
            msg += f". Скрипт написан под контракт {out['contract']}.{out['contract_minor']}, эта версия знает до {out['contract']}.{known}: обновите dawg-replay"
        issues.append(Issue("unknown_field", key, msg))

    if "contract" in out:
        declared = (out["contract"], out["contract_minor"])
        for key, since in _FIELD_SINCE.items():
            if key in data and declared < since:
                v = f"{since[0]}.{since[1]}"
                issues.append(
                    Issue(
                        "field_needs_minor",
                        key,
                        f"поле `{key}` есть в контракте с версии {v}: укажите contract = \"{v}\"",
                    )
                )
        raw_outputs = data.get("outputs")
        for i, raw in enumerate(raw_outputs if isinstance(raw_outputs, list) else []):
            if isinstance(raw, dict) and "kind" in raw and declared < (1, 5):
                issues.append(Issue("field_needs_minor", f"outputs[{i}].kind",
                                    "выходные значения (`kind`) есть в контракте с версии 1.5: укажите contract = \"1.5\""))
        raw_params = data.get("params")
        for i, raw in enumerate(raw_params if isinstance(raw_params, list) else []):
            t = raw.get("type") if isinstance(raw, dict) else None
            since = _TYPE_SINCE.get(t) if isinstance(t, str) else None
            if since and declared < since:
                v = f"{since[0]}.{since[1]}"
                issues.append(
                    Issue("type_needs_minor", f"params[{i}].type", f"тип `{t}` есть в контракте с версии {v}: укажите contract = \"{v}\"")
                )
            for key, since in _PARAM_FIELD_SINCE.items():
                if isinstance(raw, dict) and key in raw and declared < since:
                    v = f"{since[0]}.{since[1]}"
                    issues.append(Issue("field_needs_minor", f"params[{i}].{key}",
                                        f"поле `{key}` есть в контракте с версии {v}: укажите contract = \"{v}\""))
            if not isinstance(raw, dict) or declared >= (1, 3):
                continue
            if isinstance(raw.get("choices"), list) and any(isinstance(c, dict) for c in raw["choices"]):
                issues.append(Issue("field_needs_minor", f"params[{i}].choices",
                                    "подписи вариантов `{ value, title }` есть в контракте с версии 1.3: укажите contract = \"1.3\""))
            if t in ("date", "datetime") and relative.is_relative(raw.get("default"), t):
                issues.append(Issue("field_needs_minor", f"params[{i}].default",
                                    "относительная дата в `default` есть в контракте с версии 1.3: укажите contract = \"1.3\""))
            if "min_items" in raw:
                issues.append(Issue("field_needs_minor", f"params[{i}].min_items",
                                    "`min_items` есть в контракте с версии 1.3: укажите contract = \"1.3\""))

    name = data.get("name")
    if name is None:
        issues.append(Issue("missing_field", "name", "нет обязательного поля `name`"))
    elif not isinstance(name, str) or not _SLUG_RE.fullmatch(name) or len(name) > _SLUG_MAX:
        issues.append(
            Issue("bad_slug", "name", f"`name` {name!r} должен быть slug: строчные латинские буквы, цифры и `-`, до {_SLUG_MAX} символов")
        )
    else:
        out["name"] = name

    title = data.get("title")
    if title is None:
        issues.append(Issue("missing_field", "title", "нет обязательного поля `title`"))
    elif not isinstance(title, str) or not title.strip():
        issues.append(Issue("bad_value", "title", "`title` должен быть непустой строкой"))
    else:
        out["title"] = title

    desc = data.get("description")
    if desc is not None:
        if isinstance(desc, str):
            out["description"] = desc
        else:
            issues.append(Issue("bad_value", "description", "`description` должен быть строкой"))

    site = data.get("site")
    if site is not None:
        if not isinstance(site, str) or not site.strip() or "\n" in site or "\r" in site or len(site) > SITE_MAX:
            issues.append(Issue("bad_value", "site", f"`site` должен быть непустой строкой в одну строку, до {SITE_MAX} символов"))
        else:
            out["site"] = site

    tags = data.get("tags")
    if tags is not None:
        problem = _tags_problem(tags)
        if problem:
            issues.append(Issue("bad_value", "tags", problem))
        else:
            out["tags"] = tuple(tags)

    version = data.get("version")
    if version is None:
        issues.append(Issue("missing_field", "version", "нет обязательного поля `version`"))
    elif not _is_int(version) or version < 1:
        issues.append(Issue("bad_value", "version", f"`version` должен быть целым не меньше 1, получено {version!r}"))
    else:
        out["version"] = version

    timeout = data.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
    if not _is_int(timeout) or timeout < 1:
        issues.append(Issue("bad_value", "timeout_minutes", f"`timeout_minutes` должен быть целым не меньше 1, получено {timeout!r}"))
    else:
        out["timeout_minutes"] = timeout

    raw_params = data.get("params", [])
    params: list[Param] = []
    if not isinstance(raw_params, list):
        issues.append(Issue("bad_value", "params", "параметры задаются таблицами `[[params]]`"))
    else:
        seen: set[str] = set()
        for i, raw in enumerate(raw_params):
            p = _check_param(raw, i, seen, issues)
            if p is not None:
                params.append(p)
    out["params"] = tuple(params)

    raw_outputs = data.get("outputs")
    if raw_outputs is not None:
        outputs = _check_outputs(raw_outputs, issues)
        if outputs is not None:
            out["outputs"] = outputs
    return out


def _output_name_problem(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "`name` должен быть непустой строкой: имя файла в каталоге результата или шаблон, например \"prices-*.csv\""
    if len(name) > OUTPUT_NAME_MAX:
        return f"`name` длиннее {OUTPUT_NAME_MAX} символов"
    if name != name.strip() or any(c in name for c in "\n\r\t\\"):
        return "`name` задаётся в одну строку, без пробелов по краям; каталоги разделяются `/`"
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return "`name` задаётся путём внутри каталога результата, без `/` в начале"
    if any(part in ("", ".", "..") for part in name.split("/")):
        return "в `name` не бывает пустых частей пути, `.` и `..`"
    return None


def _check_value_output(raw: dict, base: str, seen: set[str], issues: list[Issue]) -> Output | None:
    """Выходное значение `kind = "value"` (контракт 1.5)."""
    n = len(issues)

    def err(code: str, key: str, msg: str) -> None:
        issues.append(Issue(code, f"{base}.{key}", msg))

    for key in raw:
        if key not in _VALUE_OUTPUT_KEYS:
            err("unknown_field", key, f"у значения нет поля `{key}`; допустимы: {', '.join(_VALUE_OUTPUT_KEYS)}")
    name = raw.get("name")
    if name is None:
        err("missing_field", "name", "нет обязательного поля `name`")
    elif not isinstance(name, str) or not _PARAM_NAME_RE.match(name):
        err("bad_value", "name", "имя значения как у параметра: латиница в нижнем регистре, цифры и `_`, с буквы")
    elif name in seen:
        err("duplicate_output", "name", f"значение `{name}` уже объявлено")
    else:
        seen.add(name)
    t = raw.get("type")
    if t is None:
        err("missing_field", "type", f"нет обязательного поля `type`; допустимы: {', '.join(VALUE_TYPES)}")
    elif t not in VALUE_TYPES:
        err("bad_type", "type", f"тип значения {t!r} не подходит; допустимы: {', '.join(VALUE_TYPES)}")
    title = raw.get("title")
    if title is not None and (
        not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title or len(title) > OUTPUT_TITLE_MAX
    ):
        err("bad_value", "title", f"`title` должен быть непустой строкой в одну строку, до {OUTPUT_TITLE_MAX} символов")
    for key in ("optional", "secret"):
        if not isinstance(raw.get(key, False), bool):
            err("bad_value", key, f"`{key}` задаётся true или false")
    choices = raw.get("choices")
    if t in CHOICE_TYPES:
        if not isinstance(choices, list) or not choices or not all(isinstance(c, str) and c for c in choices):
            err("bad_value", "choices", "у значения enum и multi_enum нужен `choices`: непустой список строк")
        elif len(set(choices)) != len(choices):
            err("bad_value", "choices", "в `choices` есть повторы")
    elif choices is not None:
        err("bad_value", "choices", "`choices` бывает только у enum и multi_enum")
    if len(issues) > n:
        return None
    return Output(name=name, title=title, optional=raw.get("optional", False), kind="value", type=t,
                  secret=raw.get("secret", False), choices=tuple(choices) if t in CHOICE_TYPES else None)


def _check_outputs(raw_outputs: Any, issues: list[Issue]) -> tuple[Output, ...] | None:
    if not isinstance(raw_outputs, list):
        issues.append(Issue("bad_value", "outputs", "выходы задаются таблицами `[[outputs]]`"))
        return None
    if len(raw_outputs) > OUTPUTS_MAX:
        issues.append(Issue("bad_value", "outputs", f"в `outputs` не больше {OUTPUTS_MAX} файлов, получено {len(raw_outputs)}"))
        return None
    n_before = len(issues)
    outputs: list[Output] = []
    seen: set[str] = set()
    values_seen: set[str] = set()
    main_at: str | None = None
    for i, raw in enumerate(raw_outputs):
        base = f"outputs[{i}]"
        if not isinstance(raw, dict):
            issues.append(Issue("output_not_table", base, "выход задаётся таблицей `[[outputs]]`"))
            continue
        n = len(issues)

        def err(code: str, key: str, msg: str) -> None:
            issues.append(Issue(code, f"{base}.{key}", msg))

        kind = raw.get("kind", "file")
        if kind not in OUTPUT_KINDS:
            err("bad_value", "kind", f"`kind` бывает {', '.join(repr(k) for k in OUTPUT_KINDS)}, получено {kind!r}")
            continue
        if kind == "value":
            out = _check_value_output(raw, base, values_seen, issues)
            if out is not None:
                outputs.append(out)
            continue

        name = raw.get("name")
        if name is None:
            err("missing_field", "name", "нет обязательного поля `name`")
        else:
            problem = _output_name_problem(name)
            if problem:
                err("bad_value", "name", problem)
            elif name in seen:
                err("duplicate_output", "name", f"файл `{name}` уже объявлен")
            else:
                seen.add(name)
                base = f"outputs[{i}]({name})"
        for key in raw:
            if key not in _OUTPUT_KEYS:
                err("unknown_field", key, f"неизвестное поле `{key}`; допустимы: {', '.join(_OUTPUT_KEYS)}")
        title = raw.get("title")
        if title is not None and (
            not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title or len(title) > OUTPUT_TITLE_MAX
        ):
            err("bad_value", "title", f"`title` должен быть непустой строкой в одну строку, до {OUTPUT_TITLE_MAX} символов")
        columns = raw.get("columns")
        if columns is not None:
            if not isinstance(columns, list) or not columns or not all(
                isinstance(c, str) and c.strip() and c == c.strip() for c in columns
            ):
                err("bad_value", "columns", "`columns` должен быть непустым списком названий колонок")
            elif len(set(columns)) != len(columns):
                err("bad_value", "columns", "в `columns` есть повторы")
        main = raw.get("main", False)
        if not isinstance(main, bool):
            err("bad_value", "main", "`main` задаётся true или false")
        elif main and main_at is not None:
            err("duplicate_main", "main", f"главный файл уже объявлен: `{main_at}`; `main = true` бывает у одного выхода")
        elif main and isinstance(name, str):
            main_at = name
        optional = raw.get("optional", False)
        if not isinstance(optional, bool):
            err("bad_value", "optional", "`optional` задаётся true или false")
        if len(issues) == n:
            outputs.append(Output(name=name, title=title, columns=tuple(columns) if columns is not None else None,
                                  main=main, optional=optional))
    if len(issues) > n_before:
        return None
    return tuple(outputs)


def _tags_problem(tags: Any) -> str | None:
    if not isinstance(tags, list):
        return "`tags` задаётся списком строк, например [\"Цены\"]"
    if len(tags) > TAGS_MAX:
        return f"в `tags` не больше {TAGS_MAX} меток, получено {len(tags)}"
    seen: set[str] = set()
    for i, tag in enumerate(tags):
        if not isinstance(tag, str) or not tag.strip() or "\n" in tag or "\r" in tag or len(tag) > TAG_MAX:
            return f"`tags[{i}]` должна быть непустой строкой в одну строку, до {TAG_MAX} символов"
        if tag in seen:
            return f"метка {tag!r} в `tags` повторяется"
        seen.add(tag)
    return None


def _check_script_block(data: dict[str, Any], issues: list[Issue]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    rp = data.get("requires-python")
    if rp is not None:
        if isinstance(rp, str):
            out["requires_python"] = rp
        else:
            issues.append(Issue("bad_value", "script.requires-python", "`requires-python` должен быть строкой"))
    deps = data.get("dependencies", [])
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        issues.append(Issue("bad_value", "script.dependencies", "`dependencies` должен быть списком строк"))
    else:
        out["dependencies"] = tuple(deps)
    return out


# --- точки входа -----------------------------------------------------------


def _unclosed(kind: str, issues: list[Issue]) -> bool:
    return any(i.code == "unclosed_block" and f"блок `{kind}`" in i.message for i in issues)


def parse_manifest(text: str) -> Manifest:
    """Читает и проверяет манифест из текста скрипта.

    Собирает все найденные ошибки и бросает ManifestError со списком.
    """
    text = text.removeprefix("\ufeff").replace("\r\n", "\n")
    issues: list[Issue] = []
    blocks = _read_blocks(text, issues)

    script_fields: dict[str, Any] = {}
    if SCRIPT_BLOCK in blocks:
        data = _parse_toml(blocks[SCRIPT_BLOCK], SCRIPT_BLOCK, issues)
        if data is not None:
            script_fields = _check_script_block(data, issues)
    elif not _unclosed(SCRIPT_BLOCK, issues):
        issues.append(Issue("missing_block", "", "нет блока `# /// script` с зависимостями (PEP 723)"))

    fields: dict[str, Any] = {}
    if MANIFEST_BLOCK in blocks:
        data = _parse_toml(blocks[MANIFEST_BLOCK], MANIFEST_BLOCK, issues)
        if data is not None:
            fields = _check_manifest(data, issues)
    elif not _unclosed(MANIFEST_BLOCK, issues):
        issues.append(Issue("missing_block", "", "нет блока манифеста `# /// dawg-script`"))

    if issues:
        raise ManifestError(issues)
    return Manifest(**fields, **script_fields)


def load_manifest(path: str | Path) -> Manifest:
    """Читает манифест из файла. Ошибки чтения файла тоже приходят как ManifestError."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ManifestError([Issue("file_not_found", "", f"файл не найден: {p}")]) from None
    except IsADirectoryError:
        raise ManifestError([Issue("file_not_found", "", f"это каталог, нужен файл script.py: {p}")]) from None
    except UnicodeDecodeError:
        raise ManifestError([Issue("bad_encoding", "", f"файл не в UTF-8: {p}")]) from None
    except OSError as e:
        raise ManifestError([Issue("file_unreadable", "", f"не удалось прочитать {p}: {e.strerror}")]) from None
    return parse_manifest(text)
