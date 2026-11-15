"""Статическое чтение и проверка манифеста скрипта (docs/contract.md, разделы 1 и 2).

Скрипт не запускается и его зависимости не ставятся: читаются только блоки
комментариев PEP 723 в начале файла.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

SUPPORTED_CONTRACTS: tuple[int, ...] = (1,)
"""Мажорные версии контракта, которые понимает эта версия kit."""

CONTRACT_MINORS: dict[int, int] = {1: 2}
"""Последняя минорная версия каждого мажора, которую знает эта версия kit."""


def contract_versions() -> list[str]:
    """Полные версии для `dawg-replay --version`: ["1.2"]."""
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

_TOP_KEYS = ("contract", "name", "title", "description", "site", "tags", "version", "timeout_minutes", "params")
# Поля, появившиеся в минорных версиях: поле -> (мажор, минор).
_FIELD_SINCE: dict[str, tuple[int, int]] = {"site": (1, 1), "tags": (1, 1)}
# Типы параметров, появившиеся в минорных версиях.
_TYPE_SINCE: dict[str, tuple[int, int]] = {"date": (1, 2), "table": (1, 2)}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_MAX_ROWS = 10000
ROW_KEY = "_row"

SITE_MAX = 40
TAGS_MAX = 5
TAG_MAX = 24
_PARAM_KEYS = (
    "name", "type", "title", "description", "required", "default", "min", "max", "max_items", "choices",
    "columns", "max_rows", "secret",
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
    choices: tuple[str, ...] | None = None
    columns: tuple[str, ...] | None = None  # table: колонки, которые обязаны быть в шапке
    max_rows: int | None = None  # table: предел строк, без поля DEFAULT_MAX_ROWS
    secret: bool = False  # table: таблица целиком секрет

    @property
    def has_default(self) -> bool:
        return self.default is not None

    @property
    def is_secret(self) -> bool:
        """Значение не хранится и не показывается: `secret` или `table` с `secret = true`."""
        return self.type == "secret" or (self.type == "table" and self.secret)

    @property
    def row_limit(self) -> int:
        return self.max_rows if self.max_rows is not None else DEFAULT_MAX_ROWS

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type, "title": self.title, "required": self.required}
        for key in ("description", "default", "min", "max", "max_items", "max_rows"):
            value = getattr(self, key)
            if value is not None:
                d[key] = value
        if self.choices is not None:
            d["choices"] = list(self.choices)
        if self.columns is not None:
            d["columns"] = list(self.columns)
        if self.secret:
            d["secret"] = True
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
            return f"`{value}` нет в choices: {', '.join(param.choices)}"
        return None
    if t == "url":
        return None if _is_url(value) else f"ожидается адрес http(s)://..., получено {value!r}"
    if t == "date":
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


def _date_problem(value: Any) -> str | None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return f"ожидается дата ГГГГ-ММ-ДД, получено {value!r}"
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return f"такой даты нет: {value}"
    return None


def _table_problem(param: Param, value: Any) -> str | None:
    """Таблица: список строк-объектов со строковыми значениями и номером `_row`."""
    if not isinstance(value, list):
        return f"ожидается таблица (список строк), получено {_type_name(value)}"
    if not value:
        return "в таблице нет строк"
    if len(value) > param.row_limit:
        return f"строк {len(value)}, а max_rows = {param.row_limit}"
    for i, row in enumerate(value):
        if not isinstance(row, dict):
            return f"строка {i}: ожидается объект, получено {_type_name(row)}"
        n = row.get(ROW_KEY)
        if not _is_int(n) or n < 2:
            return f"строка {i}: нет номера `{ROW_KEY}` (целое, шапка это строка 1)"
        for key, cell in row.items():
            if key != ROW_KEY and not isinstance(cell, str):
                return f"строка {n}, колонка «{key}»: ожидается строка, получено {_type_name(cell)}"
    if param.columns:
        missing = [c for c in param.columns if c not in value[0]]
        if missing:
            return "в шапке нет колонок: " + ", ".join(f"«{c}»" for c in missing)
        # Колонки из `columns` нужны скрипту в каждой строке: строка с паролем без логина
        # упала бы потом как неверный вход. Одно правило для файла, ввода вручную и --row.
        for row in value:
            empty = [c for c in param.columns if not str(row.get(c) or "").strip()]
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
        if t is not None and t not in (*NUMBER_TYPES, "date"):
            err("field_not_allowed", key, f"`{key}` бывает только у int, float и date")
        elif t == "date":
            problem = _date_problem(v)
            if problem:
                err("bad_value", key, f"`{key}` у date: {problem}")
        elif t == "int" and not _is_int(v):
            err("bad_value", key, f"`{key}` у int должен быть целым")
        elif not _is_number(v):
            err("bad_value", key, f"`{key}` должен быть числом")
    if t == "date":
        if isinstance(lo, str) and isinstance(hi, str) and not _date_problem(lo) and not _date_problem(hi) and lo > hi:
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
        if t is not None and t != "table":
            err("field_not_allowed", "secret", "`secret` бывает только у table; для секретной строки есть тип secret")
        elif not isinstance(secret, bool):
            err("bad_value", "secret", "`secret` должен быть true или false")

    max_items = raw.get("max_items")
    if max_items is not None:
        if t is not None and t not in LIST_TYPES:
            err("field_not_allowed", "max_items", "`max_items` бывает только у url_list и string_list")
        elif not _is_int(max_items) or max_items < 1:
            err("bad_value", "max_items", "`max_items` должен быть целым не меньше 1")

    choices = raw.get("choices")
    if t == "enum":
        if choices is None:
            err("missing_field", "choices", "у enum обязателен список `choices`")
        elif (
            not isinstance(choices, list)
            or not choices
            or not all(isinstance(c, str) and c for c in choices)
        ):
            err("bad_value", "choices", "`choices` должен быть непустым списком непустых строк")
        elif len(set(choices)) != len(choices):
            err("bad_value", "choices", "в `choices` есть повторы")
    elif choices is not None and t is not None:
        err("field_not_allowed", "choices", "`choices` бывает только у enum")

    default = raw.get("default")
    if default is not None and t == "secret":
        err("field_not_allowed", "default", "у secret не бывает `default`: секрет не хранится в скрипте")
        default = None
    elif default is not None and t == "table":
        err("field_not_allowed", "default", "у table не бывает `default`: таблица приходит из файла")
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
        choices=tuple(choices) if choices is not None else None,
        columns=tuple(columns) if columns is not None else None,
        max_rows=max_rows,
        secret=bool(secret),
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
        raw_params = data.get("params")
        for i, raw in enumerate(raw_params if isinstance(raw_params, list) else []):
            t = raw.get("type") if isinstance(raw, dict) else None
            since = _TYPE_SINCE.get(t) if isinstance(t, str) else None
            if since and declared < since:
                v = f"{since[0]}.{since[1]}"
                issues.append(
                    Issue("type_needs_minor", f"params[{i}].type", f"тип `{t}` есть в контракте с версии {v}: укажите contract = \"{v}\"")
                )

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
    return out


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
