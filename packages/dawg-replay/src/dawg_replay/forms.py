"""Форма параметров скрипта по манифесту (ТЗ 5.2, 6.4 п.2). Без веба и шаблонов.

    fields = forms.build_fields(app, "ozon-search-cards")          # первая отрисовка
    sub = forms.submit(app, "ozon-search-cards", request.form)     # «Запустить»
    if sub.ok:
        prepared = launch.prepare(app, "ozon-search-cards", values=sub.values)
    else:
        render(sub.fields)                                          # ошибки у полей, введённое сохранено

Типы и виджеты:

| тип | виджет | значение в форме |
|---|---|---|
| string | input text | строка |
| url | input url | строка |
| int, float | input number (`step` 1 или `any`, `min`, `max`) | строка |
| text | textarea | строка |
| url_list, string_list | textarea, по строке на элемент | строки через перевод строки |
| bool | checkbox | отмечен или нет; поля нет в отправке = false |
| enum | select из `choices` | строка |
| secret | input password | значение никогда не отдаётся, только «задан/не задан» |
| date | input date | строка ГГГГ-ММ-ДД |
| table | загрузка файла (отдельный запрос) | id загрузки (dawg_replay.uploads) |

Правила разбора: пустое или из пробелов значение = не задано (`is_blank` из kit),
тогда действует `default`; у списков пустые строки отбрасываются. Значения проверяет
`check_value` из kit, итоговую проверку делает `resolve` из kit, как при запуске из CLI.

Секреты: поле секрета показывает только, задан ли он. Введённое значение сразу
сохраняется в keyring (`app.secrets`), даже если в других полях ошибки: иначе
его пришлось бы вводить заново: обратно в форму секрет не отдаётся. Пустое поле
секрет не стирает.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from dawg_replay_kit import relative
from dawg_replay_kit.manifest import LIST_TYPES, Manifest, Param, check_value
from dawg_replay_kit.parameters import ParamsError, is_blank, resolve
from dawg_replay_kit.tables import TableError, columns_of, convert_types, parse_table, sheet_names

from dawg_replay.errors import ReplayError
from dawg_replay.imports import display_name
from dawg_replay.uploads import Uploads

WIDGETS = {
    "string": "text",
    "url": "url",
    "int": "number",
    "float": "number",
    "text": "textarea",
    "url_list": "textarea",
    "string_list": "textarea",
    "bool": "checkbox",
    "enum": "select",
    "secret": "password",
    "date": "date",
    "table": "table",
    "time": "time",
    "datetime": "datetime",
    "multi_enum": "checkboxes",
}
REL = "__rel"  # поле выбора относительной даты: `<имя>__rel`
REL_TIME = "__reltime"  # время к относительной дате у datetime
PREVIEW_ROWS = 5
TABLE_EXPIRED = "загруженная таблица устарела (хранится сутки), загрузите файл заново"
CHECKED = ("on", "true", "1", "yes", "да")
FORM_ERROR = "__form__"  # ошибка не конкретного поля


@dataclass
class Field:
    """Описание поля для шаблона. `value` уже в виде для отрисовки (строка или bool)."""

    name: str
    type: str
    widget: str
    title: str
    description: str | None
    required: bool
    value: str | bool | list[str]
    default: Any = None
    min: int | float | str | None = None
    max: int | float | str | None = None
    step: str | None = None
    max_items: int | None = None
    choices: list[str] | None = None
    secret_set: bool | None = None  # только для secret
    table: dict[str, Any] | None = None  # только для table: что загружено (table_info)
    columns: list[str] | None = None  # table: обязательные колонки
    max_rows: int | None = None
    secret_table: bool = False
    manual: dict[str, Any] | None = None  # table: введённое вручную при ошибке, чтобы не потерять
    inputs: list[str] | None = None  # table: способы ввода, которые объявил скрипт
    sheet: str | int | None = None  # table: лист XLSX из манифеста
    column_types: dict[str, str] | None = None  # table: типы колонок из манифеста
    choice_titles: list[tuple[str, str]] | None = None  # enum, multi_enum: (значение, подпись)
    min_items: int | None = None
    relative: str = ""  # date, datetime: выбранное выражение (`today-1`) или "" для точной даты
    rel_time: str = ""  # datetime: время к относительной дате
    rel_options: list[dict[str, str]] | None = None  # date, datetime: {"value", "title", "today"}
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Submission:
    """Разобранная форма."""

    values: dict[str, Any]  # типизированные значения без секретов, только заданные
    errors: dict[str, str]  # имя поля (или FORM_ERROR) -> текст
    raw: dict[str, str | bool]  # введённое, для повторной отрисовки (секреты пустые)
    secrets_saved: list[str] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # table -> имя файла
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)  # table -> table_info для отрисовки
    # Введённые секреты: только для сохранения в keyring, не печатаются и не отдаются.
    new_secrets: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- поля ------------------------------------------------------------------------


def display(param: Param, value: Any) -> str | bool:
    """Значение в виде для поля формы."""
    if param.type == "bool":
        return bool(value)
    if param.type == "secret" or value is None:
        return ""
    if param.type in LIST_TYPES:
        return "\n".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
    if param.type == "multi_enum":
        return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]
    if param.type in ("date", "datetime") and relative.is_relative(value, param.type):
        return ""  # выражение показывается выбором рядом, поле точной даты пустое
    if isinstance(value, float) and value.is_integer() and param.type == "float":
        return repr(value)
    return str(value)


def table_info(param: Param, upload_id: str, file: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Что показать у поля таблицы. У секретной таблицы только имя файла и число строк."""
    # Имена колонок не секрет: по ним строится ввод строк вручную. Значения секретной таблицы не показываются.
    info: dict[str, Any] = {"id": upload_id, "file": file, "count": len(rows), "secret": param.secret,
                            "columns": columns_of(rows), "preview": []}
    if not param.secret:
        info["preview"] = [[r.get(c, "") for c in info["columns"]] for r in rows[:PREVIEW_ROWS]]
    return info


def fields_for(
    manifest: Manifest,
    values: Mapping[str, Any] | None = None,
    raw: Mapping[str, str | bool] | None = None,
    errors: Mapping[str, str] | None = None,
    secret_status: Mapping[str, bool] | None = None,
    tables: Mapping[str, dict[str, Any]] | None = None,
) -> list[Field]:
    """Поля формы. Значение поля: введённое (`raw`) > `values` > `default`.
    `tables`: загруженные таблицы по имени параметра (table_info); значение поля это id загрузки."""
    values = values or {}
    raw = raw or {}
    errors = errors or {}
    secret_status = secret_status or {}
    tables = tables or {}
    out = []
    for p in manifest.params:
        if p.type == "table":
            info = tables.get(p.name)
            value = info["id"] if info else ""
        elif p.name in raw:
            value = raw[p.name] if p.type != "secret" else ""
        elif p.name in values:
            value = display(p, values[p.name])
        else:
            value = display(p, p.default)
        rel, rel_time, options = _relative_state(p, values, raw)
        out.append(
            Field(
                name=p.name,
                type=p.type,
                widget=WIDGETS.get(p.type, "text"),
                title=p.title,
                description=p.description,
                required=p.required,
                value=value,
                default=None if p.type == "secret" else p.default,
                min=p.min,
                max=p.max,
                step=("1" if p.type == "int" else "any") if p.type in ("int", "float") else None,
                max_items=p.max_items,
                choices=list(p.choices) if p.choices else None,
                secret_set=bool(secret_status.get(p.name)) if p.type == "secret" else None,
                table=tables.get(p.name) if p.type == "table" else None,
                columns=list(p.columns) if p.columns else None,
                max_rows=p.row_limit if p.type == "table" else None,
                secret_table=p.type == "table" and p.secret,
                inputs=list(p.inputs) if p.type == "table" else None,
                sheet=p.sheet if p.type == "table" else None,
                column_types=p.types or None if p.type == "table" else None,
                choice_titles=[(v, p.choice_title(v)) for v in p.choices] if p.choices else None,
                min_items=p.min_items,
                relative=rel,
                rel_time=rel_time,
                rel_options=options,
                error=errors.get(p.name),
            )
        )
    return out


def _relative_state(p: Param, values: Mapping[str, Any], raw: Mapping[str, Any]) -> tuple[str, str, list[dict[str, str]] | None]:
    """Выбор относительной даты у date и datetime: (выражение, время, варианты для выбора)."""
    if p.type not in ("date", "datetime"):
        return "", "", None
    if f"{p.name}{REL}" in raw:
        expr, t = str(raw[f"{p.name}{REL}"]), str(raw.get(f"{p.name}{REL_TIME}") or "")
    else:
        src = values[p.name] if p.name in values else p.default
        expr, t = "", ""
        if relative.is_relative(src, p.type):
            expr, _, t = str(src).partition("T")
    choices = list(relative.COMMON) + ([expr] if expr and expr not in relative.COMMON else [])
    today = {e: relative.resolve(e) for e in choices}
    options = [{"value": e, "title": relative.title(e), "today": today[e]} for e in choices]
    return expr, t, options


def build_fields(app: Any, script: str, preset: str | None = None) -> dict[str, Any]:
    """Поля для первой отрисовки. С `preset` поля заполнены значениями пресета;
    В `dropped` ключи пресета, которые текущая версия скрипта не принимает."""
    manifest = app.registry.manifest(script)
    values: dict[str, Any] = {}
    dropped: list[str] = []
    dropped_why: list[str] = []
    missing: list[str] = []
    tables: dict[str, dict[str, Any]] = {}
    if preset:
        p = app.get_preset(script, preset)
        values, dropped = dict(p.params), list(p.dropped)
        dropped_why = [p.dropped_why.get(n, f"`{n}`") for n in p.dropped]
        missing = list(p.missing.values())
        # Таблица пресета кладётся в загрузки: форма дальше работает с ней как с загруженной.
        store = Uploads(app.paths.uploads)
        secret_rows = app.presets.secret_rows(p)  # секретные таблицы лежат отдельно от значений
        for param in manifest.params:
            rows = secret_rows.get(param.name) if param.secret else values.get(param.name)
            if param.type == "table" and isinstance(rows, list):
                saved = p.secret_tables.get(param.name, {}).get("file") if param.secret else p.files.get(param.name)
                file = saved or "таблица пресета"
                uid = store.put(param.name, file, rows, secret=param.secret)
                tables[param.name] = table_info(param, uid, file, rows)
    return {
        "fields": fields_for(manifest, values=values, secret_status=app.secret_status(script), tables=tables),
        "preset": preset,
        "dropped": dropped,
        "dropped_why": dropped_why,
        "missing": missing,
    }


# --- разбор отправленной формы ---------------------------------------------------


def _getlist(form: Any, name: str) -> list[str]:
    """Все значения поля из multidict любого вида (werkzeug, starlette, dict списков, dict строк)."""
    for method in ("getlist", "getall"):
        if hasattr(form, method):
            try:
                return [str(v) for v in getattr(form, method)(name)]
            except KeyError:
                return []
    value = form.get(name) if hasattr(form, "get") else None
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def parse(
    manifest: Manifest,
    form: Any,
    *,
    require: bool = True,
    secret_set: Mapping[str, bool] | None = None,
    uploads: Uploads | None = None,
) -> Submission:
    """Разбирает форму в типизированные значения и проверяет их через kit.

    `require=False` (сохранение пресета): обязательность не проверяется.
    `secret_set`: какие секреты уже есть в keyring (для проверки обязательных).
    `uploads`: хранилище загруженных таблиц; поле таблицы несёт id загрузки.
    """
    secret_set = secret_set or {}
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    raw: dict[str, str | bool] = {}
    new_secrets: dict[str, str] = {}
    files: dict[str, str] = {}
    tables: dict[str, dict[str, Any]] = {}

    for p in manifest.params:
        items = _getlist(form, p.name)
        if p.type == "multi_enum":
            picked = [v.strip() for v in items if v.strip()]
            raw[p.name] = picked  # type: ignore[assignment]
            if not picked:
                continue  # не выбрано: действует default
            problem = check_value(p, picked)
            if problem:
                errors[p.name] = _human(p, problem)
            else:
                values[p.name] = picked
            continue
        if p.type in ("date", "datetime"):
            rel = (_getlist(form, f"{p.name}{REL}") or [""])[-1].strip()
            if rel:
                t = (_getlist(form, f"{p.name}{REL_TIME}") or [""])[-1].strip() if p.type == "datetime" else ""
                raw[p.name] = (items[-1] if items else "")
                raw[f"{p.name}{REL}"] = rel
                raw[f"{p.name}{REL_TIME}"] = t
                expr = f"{rel}T{t}" if t else rel
                problem = check_value(p, expr) if relative.is_relative(expr, p.type) else "выберите вариант из списка"
                if not problem:
                    # Выражение проходит, но сегодня может дать дату вне min/max: сказать сразу.
                    problem = check_value(p, relative.resolve(expr, p.type))
                    if problem:
                        problem = f"{relative.title(expr)} сегодня даёт {relative.resolve(expr, p.type)}: {problem}"
                if problem:
                    errors[p.name] = _human(p, problem)
                else:
                    values[p.name] = expr
                continue
        if p.type == "table":
            uid = (items[-1] if items else "").strip()
            if not uid:
                continue
            data = uploads.get(uid) if uploads is not None else None
            if data is None or data.get("param") != p.name:
                errors[p.name] = TABLE_EXPIRED
                continue
            rows, file = data["rows"], str(data.get("file") or "")
            tables[p.name] = table_info(p, uid, file, rows)
            problem = check_value(p, rows)
            if problem:
                errors[p.name] = _human(p, problem)
                continue
            values[p.name] = rows
            files[p.name] = file
            continue
        if p.type == "bool":
            checked = any(v.strip().lower() in CHECKED for v in items)
            raw[p.name] = checked
            values[p.name] = checked
            continue
        text = items[-1] if items else ""
        if p.type in ("text", *LIST_TYPES):
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        if p.type == "secret":
            raw[p.name] = ""
            if not is_blank(p, text):
                new_secrets[p.name] = text.strip()
            continue
        raw[p.name] = text
        try:
            value = _convert(p, text)
        except ValueError as e:
            errors[p.name] = str(e)
            continue
        if is_blank(p, value):
            continue  # не задано: действует default
        problem = check_value(p, value)
        if problem:
            errors[p.name] = _human(p, problem)
            continue
        values[p.name] = value

    if require:
        for p in manifest.params:
            if p.name in errors or not p.required:
                continue
            if p.type == "secret":
                if p.name not in new_secrets and not secret_set.get(p.name):
                    errors[p.name] = "обязательный секрет не задан"
            elif p.name not in values and is_blank(p, p.default):
                errors[p.name] = "загрузите таблицу" if p.type == "table" else "обязательное поле"
        if not errors:
            # Итоговая проверка тем же kit, что и при запуске. Секреты подставляются заглушкой:
            # их значения форма не знает, обязательность уже проверена выше.
            check = dict(values)
            for p in manifest.params:
                if p.type == "secret" and (p.name in new_secrets or secret_set.get(p.name)):
                    check[p.name] = "***"
            try:
                resolve(manifest, check, "форма")
            except ParamsError as e:
                errors[FORM_ERROR] = str(e)

    return Submission(values=values, errors=errors, raw=raw, new_secrets=new_secrets, files=files, tables=tables)


def _convert(p: Param, text: str) -> Any:
    t = p.type
    if t in ("int", "float"):
        s = text.strip().replace(" ", "").replace(" ", "")
        if not s:
            return None
        if t == "int":
            try:
                return int(s)
            except ValueError:
                raise ValueError(f"нужно целое число, введено «{text.strip()}»") from None
        try:
            value = float(s.replace(",", "."))
        except ValueError:
            raise ValueError(f"нужно число, введено «{text.strip()}»") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"нужно обычное число, введено «{text.strip()}»")
        return value
    if t in LIST_TYPES:
        return [line.strip() for line in text.split("\n") if line.strip()]
    return text


def _human(p: Param, problem: str) -> str:
    """Текст ошибки kit, понятный в форме."""
    if p.type in ("int", "float"):
        if p.min is not None and "меньше min" in problem:
            return f"не меньше {_num(p.min)}"
        if p.max is not None and "больше max" in problem:
            return f"не больше {_num(p.max)}"
    if p.type == "enum":
        return "выберите значение из списка"
    if p.type in LIST_TYPES and "max_items" in problem:
        return f"не больше {p.max_items} строк"
    if p.type in ("url", "url_list") and "http" in problem:
        # kit пишет значение в repr ('ozon.ru'), в форме нужны «ёлочки».
        text = problem.replace("ожидается адрес", "нужен адрес")
        return re.sub(r"получено (['\"])(.*)\1$", lambda m: f"введено «{m.group(2)}»", text)
    if p.type == "string" and "переводить строку" in problem:
        return "одна строка, без переводов строки"
    if p.type in ("date", "datetime", "time"):
        prefix = problem.split(": ", 1)[0] + ": " if " сегодня даёт " in problem else ""
        shown = (lambda v: v) if p.type == "time" else _moment
        if "раньше min" in problem:
            return f"{prefix}не раньше {shown(p.min)}"
        if "позже max" in problem:
            return f"{prefix}не позже {shown(p.max)}"
        if "такой даты нет" in problem:
            return "такой даты нет"
        if "вариант из списка" in problem:
            return problem
        return {"date": "нужна дата", "time": "нужно время ЧЧ:ММ", "datetime": "нужны дата и время"}[p.type]
    if p.type == "multi_enum":
        if "min_items" in problem:
            return f"выберите хотя бы {p.min_items}"
        if "max_items" in problem:
            return f"выберите не больше {p.max_items}"
        return "выберите значения из списка"
    if p.type == "table" and "max_rows" in problem:
        return f"{problem.split(',')[0]}, а можно не больше {p.row_limit}"
    return problem


def _date(value: Any) -> str:
    """2026-11-01 в 01.11.2026."""
    y, m, d = str(value).split("-")
    return f"{d}.{m}.{y}"


def _moment(value: Any) -> str:
    """2026-11-01 в 01.11.2026, 2026-11-01T09:30 в 01.11.2026 09:30."""
    day, _, t = str(value).partition("T")
    return _date(day) + (f" {t}" if t else "")


def _num(v: int | float) -> str:
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


# --- действия формы --------------------------------------------------------------


def submit(app: Any, script: str, form: Any) -> Submission:
    """«Запустить»: разбирает форму, сохраняет новые секреты, проверяет обязательные.

    При `ok` значения `values` передаются в `launch.prepare(app, script, values=...)`;
    секреты prepare берёт из keyring, как при запуске из CLI. При ошибках `fields`
    готовы для повторной отрисовки с введёнными значениями.
    """
    manifest = app.registry.manifest(script)
    status = app.secret_status(script)
    store = Uploads(app.paths.uploads)
    sub = parse(manifest, form, require=False, uploads=store)
    for name, value in sub.new_secrets.items():
        try:
            app.set_secret(script, name, value)
            sub.secrets_saved.append(name)
            status[name] = True
        except ReplayError as e:
            sub.errors[name] = e.message
    full = parse(manifest, form, require=True, secret_set=status, uploads=store)
    full.secrets_saved = sub.secrets_saved
    full.new_secrets = {}
    full.errors.update({k: v for k, v in sub.errors.items() if k not in full.errors})
    full.fields = fields_for(manifest, raw=full.raw, errors=full.errors, secret_status=status, tables=full.tables)
    return full


def save_preset(app: Any, script: str, preset: str, form: Any) -> Submission:
    """«Сохранить как пресет»: текущие значения без секретов. Обязательность не требуется,
    невалидные значения не сохраняются (ошибки у полей)."""
    manifest = app.registry.manifest(script)
    sub = parse(manifest, form, require=False, uploads=Uploads(app.paths.uploads))
    status = app.secret_status(script)
    if sub.ok:
        try:
            app.save_preset(script, preset, sub.values, sub.files)
        except ReplayError as e:
            sub.errors[FORM_ERROR] = e.message
    sub.fields = fields_for(manifest, raw=sub.raw, errors=sub.errors, secret_status=status, tables=sub.tables)
    return sub


def upload_table(
    app: Any, script: str, param: str, data: bytes, filename: str | None, problem: str | None = None,
    sheet: str | None = None,
) -> dict[str, Any]:
    """Загрузка файла в поле таблицы: разбор, проверка по манифесту, сохранение.
    Возвращает поле для отрисовки; ошибка разбора или проверки видна у поля.
    `problem`: файл не дошёл (размер, multipart), поле показывается с этой ошибкой."""
    manifest = app.registry.manifest(script)
    try:
        p = manifest.param(param)
    except KeyError:
        raise ReplayError("param_not_found", f"у скрипта `{script}` нет параметра `{param}`") from None
    if p.type != "table":
        raise ReplayError("param_not_found", f"параметр `{param}` не таблица")
    if "file" not in p.inputs:
        raise ReplayError("input_not_allowed", f"параметр `{param}` не принимает файл")
    name = display_name(filename)
    error = info = None
    sheets: list[str] = []
    try:
        if problem:
            raise TableError(problem[:1].lower() + problem[1:])
        if name.lower().endswith(".xlsx") and p.sheet is None:
            # Лист не объявлен: если их несколько, после загрузки человек выбирает (по умолчанию первый).
            sheets = sheet_names(data, name)
        rows = parse_table(data, name, sheet=p.sheet if p.sheet is not None else sheet)
        rows = convert_types(rows, p.types)
    except TableError as e:
        error = str(e).removeprefix(f"{name}: ").removeprefix(f"{name}, ")
    else:
        problem = check_value(p, rows)
        if problem:
            error = _human(p, problem)
        else:
            store = Uploads(app.paths.uploads)
            uid = store.put(p.name, name, rows, secret=p.secret)
            info = table_info(p, uid, name, rows)
            if len(sheets) > 1:
                store.put_source(uid, data)
                info["sheets"], info["sheet"] = sheets, sheet if sheet in sheets else sheets[0]
    (fl,) = fields_for(_only(manifest, p), errors={p.name: error} if error else None,
                       tables={p.name: info} if info else None)
    if error:
        fl.table = {"file": name, "failed": True}
        if len(sheets) > 1:
            # Лист не подошёл: выбор листа остаётся, файл заново не нужен. Пустая загрузка держит исходный файл.
            store = Uploads(app.paths.uploads)
            holder = store.put(p.name, name, [], secret=p.secret)
            store.put_source(holder, data)
            fl.table.update({"id": holder, "sheets": sheets, "sheet": sheet if sheet in sheets else sheets[0]})
    return {"field": fl}


MANUAL_FILE = "введено вручную"
_SECRET_COLUMN = re.compile(r"pass|парол|code|код|token|токен|secret|секрет|pin|ключ|key", re.IGNORECASE)


def secret_column(name: str) -> bool:
    """Колонка похожа на пароль, код или токен: в ручном вводе секретной таблицы поле пароля."""
    return bool(_SECRET_COLUMN.search(str(name)))
MANUAL_MAX_COLUMNS = 50


def table_sheet(app: Any, script: str, param: str, upload_id: str, sheet: str) -> dict[str, Any]:
    """Другой лист уже загруженного XLSX: файл перечитывается из загрузки."""
    store = Uploads(app.paths.uploads)
    data, meta = store.source(upload_id), store.get(upload_id)
    if data is None or meta is None or meta.get("param") != param:
        return upload_table(app, script, param, b"", "таблица.xlsx", problem=TABLE_EXPIRED)
    return upload_table(app, script, param, data, str(meta.get("file") or "таблица.xlsx"), sheet=sheet)


def manual_table(app: Any, script: str, param: str, columns: list[str], cells: list[str]) -> dict[str, Any]:
    """Строки, введённые в форме («Ввести вручную»): та же таблица, что из файла.

    `columns`: имена колонок; `cells`: значения по строкам подряд (len(cells) кратно числу колонок).
    Пустые строки отбрасываются, номера `_row` 2, 3, … как у файла. Таблица сохраняется как загрузка,
    дальше форма, пресет и запуск работают с ней так же, как с файлом."""
    manifest = app.registry.manifest(script)
    try:
        p = manifest.param(param)
    except KeyError:
        raise ReplayError("param_not_found", f"у скрипта `{script}` нет параметра `{param}`") from None
    if p.type != "table":
        raise ReplayError("param_not_found", f"параметр `{param}` не таблица")
    if "manual" not in p.inputs:
        raise ReplayError("input_not_allowed", f"параметр `{param}` не принимает ввод вручную")
    error = info = None
    cols = [str(c).strip() for c in columns]
    if not cols or len(cols) > MANUAL_MAX_COLUMNS:
        error = "задайте хотя бы одну колонку" if not cols else f"не больше {MANUAL_MAX_COLUMNS} колонок"
    elif any(not c for c in cols):
        error = "у каждой колонки нужно имя"
    elif any(c.startswith("_") for c in cols):
        error = "имя колонки не может начинаться с «_»"
    elif len(set(cols)) != len(cols):
        error = "имена колонок повторяются"
    elif len(cells) % len(cols):
        error = "форма прислала неполную строку, откройте ввод заново"
    rows: list[dict[str, Any]] = []
    shown: dict[int, int] = {}  # _row -> номер строки в редакторе (пустые строки там тоже видны)
    if error is None:
        for i in range(0, len(cells), len(cols)):
            values = [str(v).strip() for v in cells[i:i + len(cols)]]
            if any(values):
                shown[len(rows) + 2] = i // len(cols) + 1
                rows.append({**dict(zip(cols, values)), "_row": len(rows) + 2})
        try:
            rows = convert_types(rows, p.types)
            problem = "введите хотя бы одну строку" if not rows else check_value(p, rows)
        except TableError as e:
            problem = str(e)
        if problem:
            # В редакторе строки идут с первой, у файла с второй (шапка это строка 1): номер как на экране.
            error = re.sub(r"^строка (\d+)", lambda m: f"строка {shown.get(int(m.group(1)), m.group(1))}", _human(p, problem))
        else:
            uid = Uploads(app.paths.uploads).put(p.name, MANUAL_FILE, rows, secret=p.secret)
            info = table_info(p, uid, MANUAL_FILE, rows)
    (fl,) = fields_for(_only(manifest, p), errors={p.name: error} if error else None,
                       tables={p.name: info} if info else None)
    if error:
        fl.table = {"file": MANUAL_FILE, "failed": True}
        fl.manual = {"columns": cols, "rows": [cells[i:i + len(cols)] for i in range(0, len(cells), len(cols))]
                     if cols and not len(cells) % len(cols) else [], "open": True}
    return {"field": fl}


def _only(manifest: Manifest, p: Param) -> Manifest:
    return replace(manifest, params=(p,))
