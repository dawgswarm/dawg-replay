"""Проекты: схемы из скриптов (M6). Файл проекта, проверка, хранение версий, экспорт и импорт.

Проект это текстовый файл `<имя>.project.toml`: узлы (скрипт + пресет) и стрелки между ними.

    name = "wb-to-google"
    title = "Карточки WB в Google"
    timeout_minutes = 240

    [[nodes]]
    id = "wb1"
    script = "wb-cards"
    preset = "Магазин 1"

    [[nodes]]
    id = "google"
    script = "google-sheets-upload"
    wait = "all"

    [[edges]]
    from = "wb1"
    to = "google"
    when = "partial"
    map = [{ from = "cards.csv", to = "cards" }]

Разбор (`parse`) проверяет синтаксис и устройство файла без приложения. Проверка `Projects.check`
сверяет проект со скриптами и пресетами: ошибки не дают сохранить, предупреждения видны.
Версии хранятся в БД текстом файла, текущая версия это указатель, как у скриптов.
Запуск проекта здесь не делается.

    project = parse(text, "wb.project.toml")
    problems = app.projects.check(project)
    r = app.projects.add("wb.project.toml")
    text = app.projects.export("wb-to-google").text
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dawg_replay_kit.manifest import Manifest

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.presets import normalize as normalize_preset
from dawg_replay.edge_values import compat
from dawg_replay.transfer import MAIN, TRANSFER_TYPES, file_spec_problem, output_for, value_outputs
from dawg_replay.values import check_partial

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

if TYPE_CHECKING:
    from dawg_replay.app import App

WHENS = ("partial", "ok", "any")  # partial: готово или частично (по умолчанию), ok: только готово, any: любой исход
WAITS = ("all", "any")
ORIGINS = ("file", "editor")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
NAME_MAX = 64
TEXT_MAX = 1_000_000

TOP_KEYS = ("name", "title", "description", "timeout_minutes", "nodes", "edges")
NODE_KEYS = ("id", "script", "preset", "title", "x", "y", "wait", "values", "preset_values")
EDGE_KEYS = ("from", "to", "when", "if_file", "map", "files", "source_column", "limit", "where", "parallel",
             "on_error")
MAP_KEYS = ("from", "value", "column", "to", "rows")
WHERE_KEYS = ("column", "op", "value", "ignore_case")
ROWS = ("first", "each", "all")
ROWS_WORDS = {"first": "первая строка", "each": "каждая строка", "all": "вся колонка"}
ON_ERRORS = ("continue", "stop")
WHERE_OPS = ("equals", "not_equals", "contains", "not_contains", "gt", "lt", "empty", "not_empty")
WHERE_ALIASES = {"eq": "equals", "ne": "not_equals"}
WHERE_NEEDS_VALUE = ("equals", "not_equals", "contains", "not_contains", "gt", "lt")
LIST_PARAMS = ("string_list", "url_list", "multi_enum")
PARALLEL_MAX = 50


# --- модель ---


@dataclass(frozen=True)
class Map:
    """Соответствие стрелки: что источника идёт в вход получателя.

    file: файл результата (имя, шаблон или @main) в table/file;
    value: выходное значение источника во вход;
    file + column: колонка таблицы во вход-значение, `rows` first (первая строка), each (запуск на
    каждую строку) или all (вся колонка списком)."""

    param: str
    file: str | None = None
    value: str | None = None
    column: str | None = None
    rows: str = "first"

    @property
    def kind(self) -> str:
        if self.value is not None:
            return "value"
        return "column" if self.column is not None else "file"

    @property
    def each(self) -> bool:
        return self.kind == "column" and self.rows == "each"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "file": self.file, "value": self.value, "column": self.column,
                "param": self.param, "rows": self.rows}

    def to_file(self) -> dict[str, Any]:
        """Как в файле проекта: { from, value, column, to, rows }."""
        out: dict[str, Any] = {"value": self.value} if self.value is not None else {"from": self.file}
        if self.column is not None:
            out["column"] = self.column
            if self.rows != "first":
                out["rows"] = self.rows
        out["to"] = self.param
        return out

    @property
    def label(self) -> str:
        if self.kind == "value":
            return f"значение {self.value} → {self.param}"
        what = "главный файл" if self.file == MAIN else self.file
        if self.kind == "column":
            return f"{what}, колонка «{self.column}» ({ROWS_WORDS[self.rows]}) → {self.param}"
        return f"{what} → {self.param}"


FileMap = Map  # прежнее имя


@dataclass(frozen=True)
class Where:
    """Условие на строки таблицы стрелки, операции как у std-filter-rows."""

    column: str
    op: str
    value: str | None = None
    ignore_case: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"column": self.column, "op": self.op, "value": self.value, "ignore_case": self.ignore_case}

    def to_file(self) -> dict[str, Any]:
        out: dict[str, Any] = {"column": self.column, "op": self.op}
        if self.value is not None:
            out["value"] = self.value
        if not self.ignore_case:
            out["ignore_case"] = False
        return out


@dataclass(frozen=True)
class Node:
    id: str
    script: str
    preset: str | None = None
    values: dict[str, Any] = field(default_factory=dict)  # несекретные значения поверх пресета
    title: str | None = None
    x: float | None = None
    y: float | None = None
    wait: str = "all"
    preset_values: dict[str, Any] | None = None  # только в переносимом файле: из чего создать пресет

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "script": self.script, "preset": self.preset, "values": self.values,
                "title": self.title, "x": self.x, "y": self.y, "wait": self.wait}


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    when: str = "partial"
    if_file: str | None = None
    maps: tuple[Map, ...] = ()
    source_column: str | None = None
    limit: int | None = None  # первые N строк таблицы колонок
    where: Where | None = None  # условие на строки таблицы колонок
    parallel: int = 1  # проход по строкам: сколько запусков узла одновременно
    on_error: str = "continue"  # проход по строкам: continue или stop

    @property
    def files(self) -> tuple[Map, ...]:
        """Только файлы в table/file."""
        return tuple(m for m in self.maps if m.kind == "file")

    @property
    def each(self) -> tuple[Map, ...]:
        return tuple(m for m in self.maps if m.each)

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.source, "to": self.target, "when": self.when, "if_file": self.if_file,
                "map": [m.to_dict() for m in self.maps], "source_column": self.source_column,
                "limit": self.limit, "where": self.where.to_dict() if self.where else None,
                "parallel": self.parallel, "on_error": self.on_error}


@dataclass(frozen=True)
class Project:
    name: str
    title: str
    description: str | None = None
    timeout_minutes: int | None = None
    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise ReplayError("node_not_found", f"в проекте `{self.name}` нет узла `{node_id}`")

    def incoming(self, node_id: str) -> list[Edge]:
        """Входящие стрелки в порядке файла (в этом порядке склеиваются строки таблиц)."""
        return [e for e in self.edges if e.target == node_id]

    def outgoing(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id]

    def order(self) -> list[str]:
        """Узлы так, что источник стоит раньше получателя; при равенстве порядок файла.
        У проекта с кругом (не прошёл проверку) узлы круга идут в конце в порядке файла."""
        indeg = {n.id: 0 for n in self.nodes}
        for e in self.edges:
            if e.target in indeg and e.source in indeg:
                indeg[e.target] += 1
        out: list[str] = []
        ready = [n.id for n in self.nodes if indeg[n.id] == 0]
        pos = {n.id: i for i, n in enumerate(self.nodes)}
        while ready:
            ready.sort(key=pos.__getitem__)
            cur = ready.pop(0)
            out.append(cur)
            for e in self.outgoing(cur):
                if e.target in indeg:
                    indeg[e.target] -= 1
                    if indeg[e.target] == 0:
                        ready.append(e.target)
        out += [n.id for n in self.nodes if n.id not in out]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description,
                "timeout_minutes": self.timeout_minutes, "nodes": [n.to_dict() for n in self.nodes],
                "edges": [e.to_dict() for e in self.edges]}


@dataclass(frozen=True)
class Problem:
    level: str  # error: не сохраняется; blocker: сохраняется, но не запускается; warning: сохранить можно
    text: str
    node: str | None = None  # id узла
    edge: int | None = None  # номер стрелки в файле, с 0
    param: str | None = None  # вход узла, к которому относится

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "text": self.text, "node": self.node, "edge": self.edge, "param": self.param}

    def __str__(self) -> str:
        prefix = {"error": "ошибка: ", "blocker": "не запустится: "}.get(self.level, "предупреждение: ")
        return prefix + self.text


def run_blockers(problems: list[Problem]) -> list[Problem]:
    """Что не даёт запустить проект: ошибки и пустые обязательные входы."""
    return [p for p in problems if p.level in ("error", "blocker")]


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    current_version: int
    title: str
    description: str | None
    versions_count: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "current_version": self.current_version, "title": self.title,
                "description": self.description, "versions_count": self.versions_count,
                "created_at": self.created_at, "updated_at": self.updated_at}


@dataclass(frozen=True)
class ProjectVersion:
    name: str
    version: int
    title: str
    sha256: str
    origin: str
    added_at: str
    current: bool

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "title": self.title, "sha256": self.sha256,
                "origin": self.origin, "added_at": self.added_at, "current": self.current}


@dataclass(frozen=True)
class Stored:
    info: ProjectInfo
    version: ProjectVersion
    project: Project
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"project": self.info.to_dict(), "version": self.version.to_dict(),
                "content": self.project.to_dict(), "text": self.text}


@dataclass(frozen=True)
class SaveResult:
    status: str  # added или unchanged
    info: ProjectInfo
    version: ProjectVersion
    previous_version: int | None
    warnings: list[Problem] = field(default_factory=list)
    presets_created: list[dict[str, str]] = field(default_factory=list)  # {"node", "script", "preset"}
    presets_kept: list[dict[str, str]] = field(default_factory=list)  # уже были, не перезаписаны

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "project": self.info.to_dict(), "version": self.version.to_dict(),
                "previous_version": self.previous_version, "warnings": [w.to_dict() for w in self.warnings],
                "presets_created": self.presets_created, "presets_kept": self.presets_kept}


@dataclass(frozen=True)
class Export:
    text: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "warnings": self.warnings}


# --- разбор файла ---


class _Issues:
    def __init__(self) -> None:
        self.items: list[Problem] = []

    def err(self, text: str, node: str | None = None, edge: int | None = None) -> None:
        self.items.append(Problem("error", text, node, edge))


def invalid(label: str | Path, problems: list[Problem]) -> ReplayError:
    errors = [p for p in problems if p.level == "error"]
    return ReplayError(
        "invalid_project",
        f"{label}: проект не прошёл проверку, ошибок: {len(errors)}\n" + "\n".join(f"  {p}" for p in problems),
        [p.to_dict() for p in problems],
    )


def _str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _unknown(issues: _Issues, data: dict, allowed: tuple[str, ...], where: str, **at: Any) -> None:
    for k in data:
        if k not in allowed:
            issues.err(f"{where}: неизвестное поле `{k}`; допустимые: {', '.join(allowed)}", **at)


def parse(text: str, label: str | Path = "проект") -> Project:
    """Файл проекта в Project. Синтаксис TOML и устройство файла; скрипты и пресеты не сверяются.
    Ошибка: ReplayError invalid_project, details список проблем."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise invalid(label, [Problem("error", f"ошибка TOML: {e}")]) from None
    return _from_data(data, label)


def from_dict(data: dict[str, Any], label: str | Path = "проект") -> Project:
    """Project из `Project.to_dict()` (редактор шлёт JSON) с той же проверкой устройства, что у файла."""
    if not isinstance(data, dict):
        raise invalid(label, [Problem("error", "проект: нужен объект")])
    top = {k: v for k, v in data.items() if k in TOP_KEYS and k not in ("nodes", "edges") and v is not None}
    nodes = []
    for n in data.get("nodes") or []:
        nodes.append({k: v for k, v in n.items() if v is not None and not (k == "values" and v == {})}
                     if isinstance(n, dict) else n)
    edges = []
    for e in data.get("edges") or []:
        if not isinstance(e, dict):
            edges.append(e)
            continue
        out = {k: v for k, v in e.items() if v is not None and k not in ("map", "files", "where")}
        if out.get("when") == "partial":
            out.pop("when")
        if out.get("parallel") == 1:
            out.pop("parallel")
        if out.get("on_error") == "continue":
            out.pop("on_error")
        maps = []
        for m in e.get("map", e.get("files")) or []:
            if isinstance(m, dict) and "param" in m:
                maps.append(Map(m["param"], m.get("file"), m.get("value"), m.get("column"),
                                m.get("rows") or "first").to_file())
            else:
                maps.append(m)
        if maps:
            out["map"] = maps
        w = e.get("where")
        if isinstance(w, dict):
            out["where"] = {k: v for k, v in w.items() if v is not None and not (k == "ignore_case" and v is True)}
        elif w is not None:
            out["where"] = w
        edges.append(out)
    return _from_data({**top, "nodes": nodes, "edges": edges}, label)


def _from_data(data: dict[str, Any], label: str | Path) -> Project:
    issues = _Issues()
    _unknown(issues, data, TOP_KEYS, "проект")

    name = data.get("name")
    if not _str(name):
        issues.err("нет имени проекта: name = \"wb-to-google\"")
        name = ""
    elif len(name) > NAME_MAX or not NAME_RE.match(name):
        issues.err(f"имя проекта `{name}`: строчные латинские буквы, цифры и дефис, до {NAME_MAX} символов")
    title = data.get("title", name)
    if not _str(title):
        issues.err("title: нужна непустая строка")
        title = name
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        issues.err("description: нужна строка")
        description = None
    timeout = data.get("timeout_minutes")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1):
        issues.err("timeout_minutes: целое число минут больше нуля")
        timeout = None

    nodes = _parse_nodes(data.get("nodes", []), issues)
    edges = _parse_edges(data.get("edges", []), {n.id for n in nodes}, issues)
    if issues.items:
        raise invalid(label, issues.items)
    return Project(name, title.strip(), description, timeout, tuple(nodes), tuple(edges))


def _parse_nodes(raw: Any, issues: _Issues) -> list[Node]:
    if not isinstance(raw, list):
        issues.err("nodes: нужен список узлов [[nodes]]")
        return []
    if not raw:
        issues.err("в проекте нет узлов: добавьте [[nodes]] с id и script")
    nodes: list[Node] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            issues.err(f"узел {i}: нужна таблица [[nodes]]")
            continue
        nid = item.get("id")
        where = f"узел `{nid}`" if _str(nid) else f"узел {i}"
        at = {"node": nid if _str(nid) else None}
        _unknown(issues, item, NODE_KEYS, where, **at)
        if not _str(nid):
            issues.err(f"{where}: нет id", **at)
            continue
        if len(nid) > NAME_MAX or not NODE_ID_RE.match(nid):
            issues.err(f"{where}: id из латинских букв, цифр, `-` и `_`, до {NAME_MAX} символов", **at)
        if nid in seen:
            issues.err(f"{where}: id повторяется, у каждого узла свой", **at)
            continue
        seen.add(nid)
        script = item.get("script")
        if not _str(script):
            issues.err(f"{where}: нет script (имя зарегистрированного скрипта)", **at)
            continue
        preset = item.get("preset")
        if preset is not None and not _str(preset):
            issues.err(f"{where}: preset это имя пресета строкой", **at)
            preset = None
        title = item.get("title")
        if title is not None and not _str(title):
            issues.err(f"{where}: title нужна непустая строка", **at)
            title = None
        x, y = item.get("x"), item.get("y")
        for k, v in (("x", x), ("y", y)):
            if v is not None and not _num(v):
                issues.err(f"{where}: {k} это число (положение на холсте)", **at)
        wait = item.get("wait", "all")
        if wait not in WAITS:
            issues.err(f"{where}: wait `{wait}`: all (после всех входящих) или any (после любой)", **at)
            wait = "all"
        values = item.get("values", {})
        if not isinstance(values, dict):
            issues.err(f"{where}: values это таблица значений {{ параметр = значение }}", **at)
            values = {}
        pv = item.get("preset_values")
        if pv is not None and not isinstance(pv, dict):
            issues.err(f"{where}: preset_values это таблица значений", **at)
            pv = None
        if pv is not None and preset is None:
            issues.err(f"{where}: preset_values без preset: не знаю, как назвать пресет", **at)
        nodes.append(Node(
            nid, script.strip(), normalize_preset(preset) if preset else None, values,
            title.strip() if title else None, x if _num(x) else None, y if _num(y) else None, wait, pv,
        ))
    return nodes


def _parse_maps(item: dict, where: str, issues: _Issues, at: dict[str, Any]) -> list[Map]:
    """Поле map (или прежнее files) стрелки."""
    if "map" in item and "files" in item:
        issues.err(f"{where}: map и files сразу; files это прежнее имя map, оставьте одно", **at)
    key = "map" if "map" in item else "files"
    raw = item.get(key, [])
    if not isinstance(raw, list):
        issues.err(f"{where}: {key} это список {{ from = \"файл\", to = \"вход\" }}", **at)
        return []
    maps: list[Map] = []
    params: set[str] = set()
    for f in raw:
        if not isinstance(f, dict) or not _str(f.get("to")) or (not _str(f.get("from")) and not _str(f.get("value"))):
            issues.err(f"{where}: в {key} нужно {{ from = \"cards.csv\", to = \"вход\" }} "
                       "или {{ value = \"значение\", to = \"вход\" }}", **at)
            continue
        _unknown(issues, f, MAP_KEYS, f"{where}, {key}", **at)
        param = f["to"].strip()
        if "from" in f and "value" in f:
            issues.err(f"{where}: в вход `{param}` и from, и value: файл или значение, что-то одно", **at)
            continue
        column, rows = f.get("column"), f.get("rows")
        if "value" in f:
            if column is not None or rows is not None:
                issues.err(f"{where}: column и rows бывают только у файла-таблицы (from), не у value", **at)
                continue
            m = Map(param, value=f["value"].strip())
        else:
            bad = file_spec_problem(f["from"])
            if bad:
                issues.err(f"{where}: {bad}", **at)
                continue
            file = f["from"].strip() if f["from"] != MAIN else MAIN
            if column is not None and not _str(column):
                issues.err(f"{where}: column это название колонки строкой", **at)
                continue
            if rows is not None and column is None:
                issues.err(f"{where}: rows бывает только с column (колонка таблицы во вход)", **at)
                continue
            if rows is not None and rows not in ROWS:
                issues.err(f"{where}: rows `{rows}`: first (первая строка), each (запуск на каждую) "
                           "или all (вся колонка списком)", **at)
                continue
            m = Map(param, file=file, column=column.strip() if column else None, rows=rows or "first")
        if param in params:
            issues.err(f"{where}: во вход `{param}` этой стрелки уже что-то идёт", **at)
            continue
        params.add(param)
        maps.append(m)
    return maps


def _parse_where(raw: Any, where: str, issues: _Issues, at: dict[str, Any]) -> Where | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not _str(raw.get("column")) or not _str(raw.get("op")):
        issues.err(f"{where}: where это {{ column = \"колонка\", op = \"equals\", value = \"да\" }}", **at)
        return None
    _unknown(issues, raw, WHERE_KEYS, f"{where}, where", **at)
    op = WHERE_ALIASES.get(raw["op"], raw["op"])
    if op not in WHERE_OPS:
        issues.err(f"{where}: where op `{raw['op']}`: {', '.join(WHERE_OPS)}", **at)
        return None
    value = raw.get("value")
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
        issues.err(f"{where}: where value это строка или число", **at)
        return None
    if op in WHERE_NEEDS_VALUE and (value is None or str(value).strip() == ""):
        issues.err(f"{where}: для where op = \"{op}\" нужно value", **at)
        return None
    if op in ("gt", "lt") and _number(value) is None:
        issues.err(f"{where}: для where op = \"{op}\" value это число", **at)
        return None
    ic = raw.get("ignore_case", True)
    if not isinstance(ic, bool):
        issues.err(f"{where}: where ignore_case это true или false", **at)
        ic = True
    return Where(raw["column"].strip(), op, None if value is None else str(value), ic)


def _number(v: Any) -> float | None:
    from dawg_replay.edge_values import number

    return number(v)


def _parse_edges(raw: Any, ids: set[str], issues: _Issues) -> list[Edge]:
    if not isinstance(raw, list):
        issues.err("edges: нужен список стрелок [[edges]]")
        return []
    edges: list[Edge] = []
    pairs: dict[tuple[str, str], int] = {}
    for i, item in enumerate(raw):
        at = {"edge": i}
        if not isinstance(item, dict):
            issues.err(f"стрелка {i + 1}: нужна таблица [[edges]]", **at)
            continue
        src, dst = item.get("from"), item.get("to")
        where = f"стрелка {src} → {dst}" if _str(src) and _str(dst) else f"стрелка {i + 1}"
        _unknown(issues, item, EDGE_KEYS, where, **at)
        ok = True
        for k, v in (("from", src), ("to", dst)):
            if not _str(v):
                issues.err(f"{where}: нет {k} (id узла)", **at)
                ok = False
            elif v not in ids:
                issues.err(f"{where}: узла `{v}` нет в проекте", **at)
                ok = False
        if ok and src == dst:
            issues.err(f"{where}: стрелка из узла в него же", **at)
            ok = False
        if ok and (src, dst) in pairs:
            issues.err(f"{where}: такая стрелка уже есть (стрелка {pairs[(src, dst)] + 1}); "
                       "несколько входов перечислите в map одной стрелки", **at)
            ok = False
        when = item.get("when", "partial")
        if when not in WHENS:
            issues.err(f"{where}: when `{when}`: partial (готово или частично), ok (только готово) или any (любой исход)", **at)
            when = "partial"
        if_file = item.get("if_file")
        if if_file is not None:
            bad = file_spec_problem(if_file) if isinstance(if_file, str) else "нужно имя файла строкой"
            if bad:
                issues.err(f"{where}: if_file: {bad}", **at)
                if_file = None
        sc = item.get("source_column")
        if sc is not None and not _str(sc):
            issues.err(f"{where}: source_column это название колонки строкой", **at)
            sc = None
        maps = _parse_maps(item, where, issues, at)
        has_column = any(m.kind == "column" for m in maps)
        has_each = any(m.each for m in maps)
        limit = item.get("limit")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            issues.err(f"{where}: limit это целое число строк больше нуля", **at)
            limit = None
        cond = _parse_where(item.get("where"), where, issues, at)
        parallel = item.get("parallel", 1)
        if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= PARALLEL_MAX:
            issues.err(f"{where}: parallel это целое число от 1 до {PARALLEL_MAX}", **at)
            parallel = 1
        on_error = item.get("on_error", "continue")
        if on_error not in ON_ERRORS:
            issues.err(f"{where}: on_error `{on_error}`: continue (остальные строки идут дальше) или stop", **at)
            on_error = "continue"
        if (limit is not None or cond is not None) and not has_column:
            issues.err(f"{where}: limit и where выбирают строки таблицы для колонки; "
                       "добавьте в map {{ from = \"файл\", column = \"колонка\", to = \"вход\" }}", **at)
        if ("parallel" in item or "on_error" in item) and not has_each:
            issues.err(f"{where}: parallel и on_error задают проход по строкам, нужен rows = \"each\" в map", **at)
        if ok:
            pairs[(src, dst)] = i
            edges.append(Edge(src, dst, when, if_file, tuple(maps), sc.strip() if sc else None, limit, cond,
                              parallel, on_error))
    return edges


# --- запись файла ---


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def _value(v: Any) -> str:
    # Строка JSON это допустимая базовая строка TOML.
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)  # inf, -inf, nan и 1e+20 в TOML пишутся так же
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v if x is not None) + "]"
    if isinstance(v, dict):
        items = [f"{_key(str(k))} = {_value(x)}" for k, x in v.items() if x is not None]
        return "{ " + ", ".join(items) + " }" if items else "{}"
    return json.dumps(str(v), ensure_ascii=False)


def _line(k: str, v: Any) -> str:
    return f"{_key(k)} = {_value(v)}"


def dump(project: Project, preset_values: dict[str, dict[str, Any]] | None = None) -> str:
    """Каноничный текст файла проекта. `preset_values`: {id узла: значения} для переноса пресетов."""
    lines = [_line("name", project.name), _line("title", project.title)]
    if project.description:
        lines.append(_line("description", project.description))
    if project.timeout_minutes is not None:
        lines.append(_line("timeout_minutes", project.timeout_minutes))
    for n in project.nodes:
        lines += ["", "[[nodes]]", _line("id", n.id), _line("script", n.script)]
        if n.preset:
            lines.append(_line("preset", n.preset))
        if n.title:
            lines.append(_line("title", n.title))
        for k in ("x", "y"):
            v = getattr(n, k)
            if v is not None:
                lines.append(_line(k, int(v) if isinstance(v, float) and v.is_integer() else v))
        if n.wait != "all":
            lines.append(_line("wait", n.wait))
        if n.values:
            lines.append(_line("values", n.values))
        pv = (preset_values or {}).get(n.id)
        if pv:
            lines += ["", "[nodes.preset_values]"] + [_line(k, v) for k, v in pv.items() if v is not None]
    for e in project.edges:
        lines += ["", "[[edges]]", _line("from", e.source), _line("to", e.target)]
        if e.when != "partial":
            lines.append(_line("when", e.when))
        if e.if_file:
            lines.append(_line("if_file", e.if_file))
        if e.maps:
            lines.append(_line("map", [m.to_file() for m in e.maps]))
        if e.source_column:
            lines.append(_line("source_column", e.source_column))
        if e.limit is not None:
            lines.append(_line("limit", e.limit))
        if e.where is not None:
            lines.append(_line("where", e.where.to_file()))
        if e.parallel != 1:
            lines.append(_line("parallel", e.parallel))
        if e.on_error != "continue":
            lines.append(_line("on_error", e.on_error))
    return "\n".join(lines) + "\n"


# --- хранение и проверка ---


def _lf(text: str) -> str:
    """Переводы строк как в Unix: тот же файл с Windows даёт ту же версию."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _cycle(project: Project) -> list[str] | None:
    """Круг по стрелкам: id узлов по кругу (первый повторяется в конце) или None."""
    color: dict[str, int] = {}
    stack_path: list[str] = []

    def visit(nid: str) -> list[str] | None:
        color[nid] = 1
        stack_path.append(nid)
        for e in project.outgoing(nid):
            c = color.get(e.target, 0)
            if c == 1:
                return stack_path[stack_path.index(e.target):] + [e.target]
            if c == 0:
                found = visit(e.target)
                if found:
                    return found
        stack_path.pop()
        color[nid] = 2
        return None

    for n in project.nodes:
        if color.get(n.id, 0) == 0:
            found = visit(n.id)
            if found:
                return found
    return None


class Projects:
    def __init__(self, conn: sqlite3.Connection, app: App):
        self._db = conn
        self._app = app

    # --- чтение ---

    def _row(self, name: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ReplayError("project_not_found", f"проекта `{name}` нет; список: dawg-replay project list")
        return row

    def _info(self, row: sqlite3.Row) -> ProjectInfo:
        v = self._db.execute("SELECT text, title FROM project_versions WHERE name = ? AND version = ?",
                             (row["name"], row["current_version"])).fetchone()
        count = self._db.execute("SELECT COUNT(*) FROM project_versions WHERE name = ?", (row["name"],)).fetchone()[0]
        try:
            description = parse(v["text"], row["name"]).description
        except ReplayError:
            description = None
        return ProjectInfo(row["name"], row["current_version"], v["title"], description, count,
                           row["created_at"], row["updated_at"])

    def get(self, name: str) -> ProjectInfo:
        return self._info(self._row(name))

    def list(self) -> list[ProjectInfo]:
        return [self._info(r) for r in self._db.execute("SELECT * FROM projects ORDER BY name").fetchall()]

    @staticmethod
    def _version(row: sqlite3.Row, current: int) -> ProjectVersion:
        return ProjectVersion(row["name"], row["version"], row["title"], row["sha256"], row["origin"],
                              row["added_at"], row["version"] == current)

    def versions(self, name: str) -> list[ProjectVersion]:
        current = self._row(name)["current_version"]
        rows = self._db.execute("SELECT * FROM project_versions WHERE name = ? ORDER BY version", (name,)).fetchall()
        return [self._version(r, current) for r in rows]

    def _version_row(self, name: str, version: int | None) -> tuple[sqlite3.Row, int]:
        current = self._row(name)["current_version"]
        v = current if version is None else version
        row = self._db.execute("SELECT * FROM project_versions WHERE name = ? AND version = ?", (name, v)).fetchone()
        if row is None:
            have = ", ".join(str(x.version) for x in self.versions(name))
            raise ReplayError("version_not_found", f"у проекта `{name}` нет версии {v}; есть: {have}")
        return row, current

    def load(self, name: str, version: int | None = None) -> Stored:
        row, current = self._version_row(name, version)
        return Stored(self.get(name), self._version(row, current), parse(row["text"], f"проект {name} v{row['version']}"),
                      row["text"])

    def using(self, script: str) -> list[str]:
        """Проекты, в текущей версии которых есть узел с этим скриптом."""
        out = []
        for info in self.list():
            try:
                if any(n.script == script for n in self.load(info.name).project.nodes):
                    out.append(info.name)
            except ReplayError:
                continue
        return out

    def node_title(self, node: Node) -> str:
        if node.title:
            return node.title
        try:
            base = self._app.registry.get(node.script).title or node.script
        except ReplayError:
            base = node.script
        return f"{base}: {node.preset}" if node.preset else base

    # --- проверка ---

    def _manifest(self, script: str) -> Manifest | None:
        try:
            self._app.registry.get(script)
            return self._app.registry.manifest(script)
        except ReplayError:
            return None

    def check(self, project: Project, importing: bool = False) -> list[Problem]:
        """Сверка со скриптами и пресетами. `importing`: пресеты из preset_values будут созданы."""
        out: list[Problem] = []

        def err(text: str, **at: Any) -> None:
            out.append(Problem("error", text, **at))

        def warn(text: str, **at: Any) -> None:
            out.append(Problem("warning", text, **at))

        manifests: dict[str, Manifest | None] = {}
        covered: dict[str, set[str]] = {}  # узел -> параметры со значением из пресета или values
        for n in project.nodes:
            m = manifests.setdefault(n.script, self._manifest(n.script))
            where = f"узел `{n.id}`"
            if m is None:
                err(f"{where}: скрипт `{n.script}` не зарегистрирован", node=n.id)
                continue
            have: set[str] = set()
            if n.preset:
                try:
                    preset = self._app.presets.get(n.script, n.preset, m)
                except ReplayError:
                    if importing and n.preset_values is not None:
                        have |= {k for k in n.preset_values if not self._secret(m, k)}
                        for problem in check_partial(m, {k: v for k, v in n.preset_values.items()
                                                         if not self._secret(m, k)}, allow_secrets=False):
                            err(f"{where}: preset_values: {problem}", node=n.id)
                    else:
                        err(f"{where}: у скрипта `{n.script}` нет пресета «{n.preset}»", node=n.id)
                else:
                    have |= set(preset.params) | set(preset.results) | set(preset.secret_tables) | set(preset.file_params)
                    if preset.dropped:  # нехватку обязательных значений смотрим ниже, с учётом стрелок
                        why = "; ".join(preset.dropped_why.get(k, f"`{k}`") for k in preset.dropped)
                        warn(f"{where}: пресет «{preset.name}» сохранён для другой версии скрипта, "
                             f"текущая не принимает: {why}", node=n.id)
            for problem in check_partial(m, n.values, allow_secrets=False):
                err(f"{where}: values: {problem}", node=n.id)
            have |= set(n.values)
            covered[n.id] = have

        into: dict[tuple[str, str], list[tuple[int, Edge, Map]]] = {}
        each_into: dict[str, list[tuple[int, Edge, Map]]] = {}
        for i, e in enumerate(project.edges):
            where = f"стрелка {e.source} → {e.target}"
            src_m, dst_m = manifests.get(project.node(e.source).script), manifests.get(project.node(e.target).script)
            if e.if_file and src_m is not None:
                declared, o = output_for(src_m, e.if_file)
                if declared and o is None:
                    warn(f"{where}: if_file `{e.if_file}` не объявлен в выходах скрипта `{src_m.name}`", edge=i)
                elif o is not None and not o.optional:
                    warn(f"{where}: `{o.name}` у скрипта `{src_m.name}` обязательный выход, "
                         "с if_file на него стрелка будет активна всегда", edge=i)
            tables = 0
            for f in e.maps:
                into.setdefault((e.target, f.param), []).append((i, e, f))
                if f.each:
                    each_into.setdefault(e.target, []).append((i, e, f))
                if dst_m is None:
                    continue
                try:
                    p = dst_m.param(f.param)
                except KeyError:
                    err(f"{where}: у скрипта `{dst_m.name}` нет входа `{f.param}`", edge=i, param=f.param)
                    continue
                at = {"edge": i, "param": f.param}
                if f.kind == "value":
                    self._check_value_map(project, e, f, p, src_m, err, warn, at)
                    continue
                if f.kind == "column":
                    self._check_column_map(e, f, p, src_m, err, warn, at)
                    continue
                if p.type not in TRANSFER_TYPES:
                    err(f"{where}: вход `{f.param}` типа {p.type}: файл идёт только в table или file; "
                        f"значение из колонки: {{ from = \"{f.file}\", column = \"колонка\", to = \"{f.param}\" }}", **at)
                    continue
                if p.is_secret:
                    err(f"{where}: вход `{f.param}` секретный, передавать в него файл нельзя", **at)
                    continue
                if p.type == "table":
                    tables += 1
                declared, o = output_for(src_m, f.file)
                if declared and o is None:
                    what = "главный файл" if f.file == MAIN else f"файл `{f.file}`"
                    warn(f"{where}: {what} не объявлен в выходах скрипта `{src_m.name}`", **at)
                elif o is not None and p.type == "table" and p.columns and getattr(o, "columns", None):
                    lack = [c for c in p.columns if c not in tuple(o.columns)]
                    if lack:
                        cols = ", ".join(f"`{c}`" for c in lack)
                        word = "колонки" if len(lack) == 1 else "колонок"
                        warn(f"{where}: в `{o.name}` нет {word} {cols}, нужны параметру `{f.param}`", **at)
            if e.source_column and dst_m is not None and tables == 0:
                warn(f"{where}: source_column действует только на таблицы, а таблиц стрелка не передаёт", edge=i)

        for (target, param), items in into.items():
            m = manifests.get(project.node(target).script)
            if m is None or len(items) < 2:
                continue
            try:
                p = m.param(param)
            except KeyError:
                continue
            names = ", ".join(f"{e.source} → {e.target}" for _, e, _ in items)
            if any(f.kind != "file" for _, _, f in items):
                err(f"узел `{target}`: во вход `{param}` идут несколько стрелок: {names}; значение берётся из одной",
                    node=target, edge=items[1][0], param=param)
            elif p.type == "file" and not p.multiple:
                err(f"узел `{target}`: в параметр `{param}` (один файл) идут несколько стрелок: {names}",
                    node=target, edge=items[1][0], param=param)
            elif p.type == "table":
                cols = []
                for _, e, f in items:
                    _, o = output_for(manifests.get(project.node(e.source).script), f.file)
                    cols.append(tuple(o.columns) if o is not None and getattr(o, "columns", None) else None)
                known = [c for c in cols if c is not None]
                if len(set(known)) > 1:
                    warn(f"узел `{target}`: в таблицу `{param}` идут файлы с разными колонками; "
                         "при склейке шаг не стартует", node=target)

        for target, items in each_into.items():
            tables = {(e.source, f.file) for _, e, f in items}
            if len(tables) > 1:
                what = "; ".join(f"{src}: {'главный файл' if file == MAIN else file}" for src, file in sorted(tables))
                err(f"узел `{target}`: проход по строкам идёт по одной таблице одного узла, а здесь несколько ({what}). "
                    "Сведите таблицы в одну узлом-скриптом", node=target, edge=items[1][0])

        for n in project.nodes:
            m = manifests.get(n.script)
            if m is None:
                continue
            fed = {f.param for e in project.incoming(n.id) for f in e.maps}
            for p in m.params:
                if (p.required and p.type != "secret" and p.default is None
                        and p.name not in covered.get(n.id, set()) and p.name not in fed):
                    out.append(Problem("blocker", f"узел `{n.id}`: не задан «{p.title}»", node=n.id, param=p.name))

        cyc = _cycle(project)
        if cyc:
            err("круг: " + " → ".join(cyc) + ". Циклов в проекте быть не может; повторы делайте внутри узла-скрипта")
        return out

    def _check_value_map(self, project: Project, e: Edge, f: Map, p: Any, src_m: Manifest | None,
                         err: Callable[..., None], warn: Callable[..., None], at: dict[str, Any]) -> None:
        where = f"стрелка {e.source} → {e.target}"
        if p.type in TRANSFER_TYPES:
            err(f"{where}: вход `{f.param}` типа {p.type}: в него идёт файл {{ from = \"файл\", to = \"{f.param}\" }}, "
                "а не значение", **at)
            return
        if any(m.each for x in project.incoming(e.source) for m in x.maps):
            err(f"{where}: у узла `{e.source}` проход по строкам, его значения собраны в таблицу values.csv; "
                f"возьмите колонку: {{ from = \"values.csv\", column = \"{f.value}\", to = \"{f.param}\" }}", **at)
            return
        if src_m is None:
            return
        out = next((o for o in value_outputs(src_m) if o.name == f.value), None)
        if out is None:
            have = ", ".join(f"`{o.name}`" for o in value_outputs(src_m))
            err(f"{where}: у скрипта `{src_m.name}` нет выходного значения `{f.value}`; "
                + (f"есть: {have}" if have else "значения объявляются в [[outputs]] с kind = \"value\" (контракт 1.5)"),
                **at)
            return
        if getattr(out, "secret", False) and not p.is_secret:
            err(f"{where}: значение `{f.value}` секретное, а вход «{p.title}» нет: оно попало бы в историю запусков",
                **at)
            return
        level = compat(getattr(out, "type", None), p)
        if level == "error":
            err(f"{where}: значение `{f.value}` ({out.type}) не подходит входу «{p.title}» ({p.type})", **at)
        elif level == "warning":
            warn(f"{where}: значение `{f.value}` ({out.type}) во вход «{p.title}» ({p.type}): "
                 "подойдёт ли, видно при старте шага", **at)

    @staticmethod
    def _check_column_map(e: Edge, f: Map, p: Any, src_m: Manifest | None,
                          err: Callable[..., None], warn: Callable[..., None], at: dict[str, Any]) -> None:
        where = f"стрелка {e.source} → {e.target}"
        if p.type in TRANSFER_TYPES:
            err(f"{where}: вход `{f.param}` типа {p.type}: колонка идёт во вход-значение; "
                f"таблица целиком: {{ from = \"{f.file}\", to = \"{f.param}\" }} без column", **at)
            return
        if f.rows == "all" and p.type not in LIST_PARAMS:
            err(f"{where}: rows = \"all\" (вся колонка списком) только для входов-списков "
                f"({', '.join(LIST_PARAMS)}), а «{p.title}» типа {p.type}", **at)
            return
        declared, o = output_for(src_m, f.file)
        what = "главный файл" if f.file == MAIN else f"файл `{f.file}`"
        if declared and o is None:
            warn(f"{where}: {what} не объявлен в выходах скрипта `{src_m.name}`", **at)
            return
        cols = tuple(getattr(o, "columns", None) or ()) if o is not None else ()
        if cols and f.column not in cols:
            warn(f"{where}: в `{o.name}` не объявлена колонка «{f.column}»; объявлены: "
                 + ", ".join(f"«{c}»" for c in cols), **at)
        if cols and e.where is not None and e.where.column not in cols:
            warn(f"{where}: в `{o.name}` не объявлена колонка «{e.where.column}» из where", **at)

    @staticmethod
    def _secret(m: Manifest, name: str) -> bool:
        try:
            return m.param(name).is_secret
        except KeyError:
            return False

    def check_source(self, source: str | Path) -> tuple[Project, list[Problem]]:
        """Для `project check`: файл (если такой есть) или имя проекта."""
        path = Path(source)
        if path.is_file():
            text = self._read(path)
            project = parse(text, path)
            return project, self.check(project, importing=any(n.preset_values is not None for n in project.nodes))
        try:
            project = self.load(str(source)).project
        except ReplayError as e:
            if e.code == "project_not_found":
                raise ReplayError("project_not_found", f"нет ни файла `{source}`, ни проекта с таким именем") from None
            raise
        return project, self.check(project)

    # --- изменение ---

    @staticmethod
    def _read(path: Path) -> str:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ReplayError("file_not_found", f"файл не найден: {path}") from None
        except OSError as e:
            raise ReplayError("file_unreadable", f"не удалось прочитать {path}: {e.strerror}") from None
        if len(data) > TEXT_MAX:
            raise ReplayError("file_too_big", f"{path}: файл проекта больше {TEXT_MAX // 1000} КБ")
        try:
            return _lf(data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            raise ReplayError("bad_encoding", f"файл не в UTF-8: {path}") from None

    def add(self, file: str | Path) -> SaveResult:
        """Новый проект из файла. Тот же проект с тем же содержимым: unchanged; с другим: project_exists."""
        return self._save_file(Path(file), create=True)

    def update(self, file: str | Path) -> SaveResult:
        """Новая версия существующего проекта из файла."""
        return self._save_file(Path(file), create=False)

    def _save_file(self, path: Path, create: bool) -> SaveResult:
        text = self._read(path)
        project = parse(text, path)
        exists = self._exists(project.name)
        if create and exists and self._sha(self._stored_text(text, project)) != self._current_sha(project.name):
            raise ReplayError("project_exists", f"проект `{project.name}` уже есть; новая версия из файла: "
                                                f"dawg-replay project update {path}")
        if not create and not exists:
            raise ReplayError("project_not_found", f"проекта `{project.name}` нет; добавить: dawg-replay project add {path}")
        return self._save(project, text, path, origin="file", base_version=None)

    def save(self, source: Project | str, base_version: int | None = None, origin: str = "editor",
             create: bool = False) -> SaveResult:
        """Сохранение с холста или из текста. `base_version`: версия, от которой шла правка; если текущая
        уже другая (агент успел поправить файл), ошибка project_changed. `create`: можно создать новый проект."""
        if origin not in ORIGINS:
            raise ReplayError("internal", f"origin: {', '.join(ORIGINS)}")
        if isinstance(source, Project):
            project, text = source, dump(source)
            project = parse(text, "проект")  # та же проверка устройства, что у файла
        else:
            text = _lf(source)
            project = parse(text, "проект")
        if not create and not self._exists(project.name):
            raise ReplayError("project_not_found", f"проекта `{project.name}` нет")
        return self._save(project, text, "проект", origin=origin, base_version=base_version)

    def _exists(self, name: str) -> bool:
        return self._db.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone() is not None

    def _current_sha(self, name: str) -> str | None:
        row = self._db.execute(
            "SELECT v.sha256 FROM projects p JOIN project_versions v ON v.name = p.name AND v.version = p.current_version"
            " WHERE p.name = ?", (name,)).fetchone()
        return row["sha256"] if row else None

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _stored_text(text: str, project: Project) -> str:
        """Версия хранит файл как есть; значения пресетов для переноса в версию не попадают."""
        if any(n.preset_values is not None for n in project.nodes):
            return dump(replace(project, nodes=tuple(replace(n, preset_values=None) for n in project.nodes)))
        return text

    def _save(self, project: Project, text: str, label: str | Path, origin: str,
              base_version: int | None) -> SaveResult:
        importing = any(n.preset_values is not None for n in project.nodes)
        problems = self.check(project, importing=importing)
        if any(p.level == "error" for p in problems):
            raise invalid(label, problems)
        warnings = [p for p in problems if p.level != "error"]  # и пустые обязательные входы
        stored = self._stored_text(text, project)
        sha = self._sha(stored)
        exists = self._exists(project.name)
        current = self._row(project.name)["current_version"] if exists else None
        if base_version is not None and current is not None and base_version != current:
            raise ReplayError("project_changed", f"проект `{project.name}` уже изменён: текущая версия v{current}, "
                                                 f"правка шла от v{base_version}. Откройте текущую версию и повторите",
                              {"current_version": current})

        created, kept, notes = self._import_presets(project) if importing else ([], [], [])
        warnings += notes
        if exists and sha == self._current_sha(project.name):
            row, cur = self._version_row(project.name, None)
            return SaveResult("unchanged", self.get(project.name), self._version(row, cur), current, warnings,
                              created, kept)

        now = utcnow()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            top = self._db.execute("SELECT MAX(version) FROM project_versions WHERE name = ?",
                                   (project.name,)).fetchone()[0]
            version = (top or 0) + 1
            if top is None:
                self._db.execute("INSERT INTO projects (name, current_version, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                 (project.name, version, now, now))
            else:
                self._db.execute("UPDATE projects SET current_version = ?, updated_at = ? WHERE name = ?",
                                 (version, now, project.name))
            self._db.execute(
                "INSERT INTO project_versions (name, version, title, sha256, text, origin, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project.name, version, project.title, sha, stored, origin, now))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        row, cur = self._version_row(project.name, version)
        return SaveResult("added", self.get(project.name), self._version(row, cur), current, warnings, created, kept)

    def _import_presets(self, project: Project) -> tuple[list[dict[str, str]], list[dict[str, str]], list[Problem]]:
        """Пресеты из preset_values: недостающие создаются, существующие не перезаписываются."""
        created: list[dict[str, str]] = []
        kept: list[dict[str, str]] = []
        notes: list[Problem] = []
        done: set[tuple[str, str]] = set()
        for n in project.nodes:
            if n.preset_values is None or not n.preset or (n.script, n.preset) in done:
                continue
            done.add((n.script, n.preset))
            m = self._app.registry.manifest(n.script)
            rec = {"node": n.id, "script": n.script, "preset": n.preset}
            try:
                self._app.presets.get(n.script, n.preset, m)
            except ReplayError:
                pass
            else:
                kept.append(rec)
                continue
            values = {k: v for k, v in n.preset_values.items() if not self._secret(m, k)}
            preset = self._app.presets.save(n.script, n.preset, m, values)
            created.append(rec)
            empty = [p.title for p in m.params if p.is_secret and p.type != "secret" and p.required
                     and p.name not in preset.secret_tables and p.name not in preset.file_params]
            secrets = [p for p in m.params if p.type == "secret" and self._app.secrets.get_secret(n.script, p.name) is None]
            if empty:
                titles = ", ".join(f"«{t}»" for t in empty)
                notes.append(Problem("warning", f"узел `{n.id}`: пресет «{n.preset}» создан без секретных значений "
                                                f"{titles}, заполните пресет", node=n.id))
            if secrets:
                names = ", ".join(f"`{p.name}`" for p in secrets)
                notes.append(Problem("warning", f"узел `{n.id}`: у скрипта `{n.script}` не заданы секреты {names}: "
                                                f"dawg-replay secret set {n.script} <параметр>", node=n.id))
        return created, kept, notes

    def rollback(self, name: str, version: int) -> ProjectVersion:
        """Текущей становится прошлая версия. Новая правка после отката получает следующий номер."""
        self._version_row(name, version)
        self._db.execute("UPDATE projects SET current_version = ?, updated_at = ? WHERE name = ?",
                         (version, utcnow(), name))
        row, current = self._version_row(name, None)
        return self._version(row, current)

    def export(self, name: str, version: int | None = None, preset_values: bool = True) -> Export:
        """Файл проекта. С `preset_values` в него кладутся несекретные значения пресетов узлов
        (каноничный текст); без них текст версии как сохранён."""
        stored = self.load(name, version)
        if not preset_values:
            return Export(stored.text)
        values: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        for n in stored.project.nodes:
            if not n.preset:
                continue
            m = self._manifest(n.script)
            if m is None:
                warnings.append(f"узел `{n.id}`: скрипта `{n.script}` нет, пресет не перенесён")
                continue
            try:
                preset = self._app.presets.get(n.script, n.preset, m)
            except ReplayError:
                warnings.append(f"узел `{n.id}`: пресета «{n.preset}» нет, перенести нечего")
                continue
            values[n.id] = {**preset.params, **preset.results}
            skipped = [m.param(k).title for k in (*preset.file_params, *preset.secret_tables)]
            if skipped:
                titles = ", ".join(f"«{t}»" for t in skipped)
                warnings.append(f"узел `{n.id}`: файлы и секретные таблицы пресета «{n.preset}» не переносятся: {titles}")
        return Export(dump(stored.project, values), warnings)

    def remove(self, name: str) -> list[ProjectVersion]:
        """Удаляет проект и все версии. Пресеты и скрипты остаются."""
        versions = self.versions(name)
        self._db.execute("DELETE FROM projects WHERE name = ?", (name,))
        return versions
