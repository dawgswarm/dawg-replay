"""Значения параметров скрипта (docs/contract.md, разделы 2 и 3).

Два источника:

- запуск из приложения: JSON из файла `DAWG_SCRIPT_PARAMS`;
- автономный запуск: `--params file.json`, аргументы вида `--max-pages 3`,
  секреты из переменных `DAWG_SECRET_<ИМЯ>`.

В обоих случаях значения проходят `check_value` из манифеста.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from dawg_replay_kit.manifest import LIST_TYPES, ROW_KEY, Manifest, Param, check_value

SECRET_ENV_PREFIX = "DAWG_SECRET_"
# Ячейки секретной таблицы короче этого не маскируются: «1», «да», «RU» встречаются
# в событиях повсюду, их замена на *** испортила бы весь вывод и ничего бы не скрыла.
SECRET_CELL_MIN = 4
CLI_SOURCE = "аргументы командной строки"


class ParamsError(Exception):
    """Значения параметров не подходят. Текст ошибки готов для показа человеку."""


class Params:
    """Проверенные значения параметров.

    Доступ: `params.max_pages` или `params["max_pages"]`.
    Необязательный параметр без значения и без default равен None.
    Методов у объекта мало (`get`, `to_dict`, `secret_values`), чтобы атрибуты
    не перекрывали имена параметров; при совпадении работает `params["get"]`.
    """

    def __init__(self, manifest: Manifest, values: dict[str, Any]):
        object.__setattr__(self, "_manifest", manifest)
        object.__setattr__(self, "_values", values)

    def __getitem__(self, name: str) -> Any:
        if name not in self._values:
            self._manifest.param(name)  # KeyError для неизвестного имени
            return None
        return self._values[name]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            known = ", ".join(p.name for p in self._manifest.params) or "нет"
            raise AttributeError(f"параметра `{name}` нет в манифесте; объявлены: {known}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("параметры только для чтения")

    def __iter__(self) -> Iterator[str]:
        return (p.name for p in self._manifest.params)

    def __len__(self) -> int:
        return len(self._manifest.params)

    def get(self, name: str, default: Any = None) -> Any:
        value = self._values.get(name)
        return default if value is None else value

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and any(p.name == name for p in self._manifest.params)

    def secret_values(self) -> list[str]:
        """Что маскировать в событиях: секреты и ячейки таблиц с `secret = true`."""
        out: list[str] = []
        for p in self._manifest.params:
            v = self._values.get(p.name)
            if p.type == "secret" and isinstance(v, str) and v:
                out.append(v)
            elif p.type == "table" and p.secret and isinstance(v, list):
                out += [
                    cell
                    for row in v if isinstance(row, dict)
                    for key, cell in row.items()
                    if key != ROW_KEY and isinstance(cell, str) and len(cell.strip()) >= SECRET_CELL_MIN
                ]
        return out

    def to_dict(self, mask_secrets: bool = True) -> dict[str, Any]:
        """Заданные значения. Секреты и секретные таблицы по умолчанию заменены на `***`."""
        out = {}
        for p in self._manifest.params:
            if p.name in self._values:
                out[p.name] = "***" if mask_secrets and p.is_secret else self._values[p.name]
        return out

    def __repr__(self) -> str:
        return f"Params({self.to_dict()!r})"


TEXT_TYPES = ("string", "text", "url", "secret", "enum", "date")


def is_blank(param: Param, value: Any) -> bool:
    """Значение считается незаданным: None, строка из одних пробелов (string, text, url,
    secret, enum) или пустой список (url_list, string_list).

    Единственное место этого правила: им пользуются и kit, и приложение (через resolve).
    """
    if value is None:
        return True
    if param.type in TEXT_TYPES:
        return isinstance(value, str) and not value.strip()
    if param.type in LIST_TYPES or param.type == "table":
        return isinstance(value, (list, tuple)) and len(value) == 0
    return False


def resolve(manifest: Manifest, given: Mapping[str, Any], source: str) -> Params:
    """Проверяет значения и подставляет default. Все ошибки собираются в одну ParamsError.

    Пустое значение (`is_blank`) считается незаданным: берётся default, у обязательного
    параметра без default это ошибка, у необязательного значения нет.
    """
    problems: list[str] = []
    known = {p.name for p in manifest.params}
    for name in given:
        if name not in known:
            problems.append(f"`{name}`: такого параметра нет в манифесте")

    values: dict[str, Any] = {}
    for p in manifest.params:
        if p.name in given and not is_blank(p, given[p.name]):
            value = given[p.name]
        elif p.has_default and not is_blank(p, p.default):
            value = p.default
        elif p.required:
            hint = _missing_hint(p, source)
            if p.name in given and given[p.name] is not None:
                hint += " (пустое значение не считается)"
            problems.append(f"`{p.name}`: {hint}")
            continue
        else:
            continue
        problem = check_value(p, value)
        if problem:
            problems.append(f"`{p.name}`: {problem}")
            continue
        if p.type == "float":
            value = float(value)
        elif p.type in LIST_TYPES:
            value = list(value)
        elif p.type == "table":
            value = [dict(row) for row in value]
        values[p.name] = value

    if problems:
        raise ParamsError(f"параметры не подходят ({source}):\n" + "\n".join(f"  {x}" for x in problems))
    return Params(manifest, values)


def _missing_hint(p: Param, source: str) -> str:
    if source != CLI_SOURCE:
        return f"обязательный параметр «{p.title}» не задан"
    if p.type == "secret":
        return f"обязательный секрет «{p.title}» не задан, передайте его переменной {secret_env_name(p.name)}"
    if p.type == "table":
        return f"обязательная таблица «{p.title}» не задана, передайте {cli_flag(p.name)} файл.csv или файл.xlsx"
    return f"обязательный параметр «{p.title}» не задан, передайте {cli_flag(p.name)} или ключ в --params"


def from_file(manifest: Manifest, path: str | Path) -> Params:
    """Режим приложения: значения из JSON `DAWG_SCRIPT_PARAMS`, секреты уже подставлены."""
    return resolve(manifest, _read_json(path), "DAWG_SCRIPT_PARAMS")


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ParamsError(f"файл параметров не найден: {path}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ParamsError(f"не удалось прочитать файл параметров {path}: {e}") from None
    except json.JSONDecodeError as e:
        raise ParamsError(f"файл параметров {path} не JSON: {e}") from None
    if not isinstance(data, dict):
        raise ParamsError(f"файл параметров {path}: ожидается JSON-объект")
    return data


# --- автономный режим --------------------------------------------------------


def cli_flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def secret_env_name(name: str) -> str:
    return SECRET_ENV_PREFIX + name.upper()


class _ArgParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise ParamsError(f"аргументы командной строки: {message}\n\n{self.format_usage()}")


def _parse_bool(text: str) -> bool:
    low = text.strip().lower()
    if low in ("true", "1", "yes", "y", "да"):
        return True
    if low in ("false", "0", "no", "n", "нет"):
        return False
    raise argparse.ArgumentTypeError(f"ожидается true или false, получено {text!r}")


def _parse_int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается целое, получено {text!r}") from None


def _parse_float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается число, получено {text!r}") from None


def build_arg_parser(manifest: Manifest, prog: str | None = None) -> argparse.ArgumentParser:
    parser = _ArgParser(
        prog=prog,
        description=manifest.title,
        epilog=_secrets_epilog(manifest),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--params", metavar="FILE", help="JSON-файл со значениями параметров")
    for p in manifest.params:
        if p.type == "secret":
            continue
        help_text = p.title + (f". {p.description}" if p.description else "")
        if p.has_default:
            help_text += f" (по умолчанию {p.default!r})"
        if p.required and not p.has_default:
            help_text += " (обязательный)"
        kwargs: dict[str, Any] = {"dest": p.name, "default": None, "help": help_text}
        if p.type == "bool":
            kwargs["type"] = _parse_bool
            kwargs["metavar"] = "true|false"
        elif p.type == "int":
            kwargs["type"] = _parse_int
        elif p.type == "float":
            kwargs["type"] = _parse_float
        elif p.type == "enum":
            kwargs["metavar"] = "{" + ",".join(p.choices or ()) + "}"
        elif p.type in LIST_TYPES:
            kwargs["action"] = "extend"
            kwargs["nargs"] = "+"
        elif p.type == "date":
            kwargs["metavar"] = "ГГГГ-ММ-ДД"
        elif p.type == "table":
            kwargs["metavar"] = "FILE"
            kwargs["help"] = help_text + ". Файл .csv или .xlsx, первая строка шапка"
        parser.add_argument(cli_flag(p.name), **kwargs)
    return parser


def _secrets_epilog(manifest: Manifest) -> str | None:
    secrets = [p for p in manifest.params if p.type == "secret"]
    if not secrets:
        return None
    lines = ["секреты передаются переменными окружения:"]
    lines += [f"  {secret_env_name(p.name)}  {p.title}" for p in secrets]
    return "\n".join(lines)


def from_cli(
    manifest: Manifest,
    argv: Sequence[str],
    environ: Mapping[str, str],
    prog: str | None = None,
) -> Params:
    """Автономный режим. Порядок: default, затем --params, затем аргументы; секреты из окружения."""
    parser = build_arg_parser(manifest, prog)
    ns = parser.parse_args(list(argv))

    given: dict[str, Any] = {}
    if ns.params:
        given.update(_read_json(ns.params))
        leaked = [p.name for p in manifest.params if p.type == "secret" and p.name in given]
        if leaked:
            hint = ", ".join(secret_env_name(n) for n in leaked)
            raise ParamsError(f"секреты в файле параметров не принимаются, передайте их переменными: {hint}")
    for p in manifest.params:
        if p.type == "secret":
            value = environ.get(secret_env_name(p.name))
            if value:
                given[p.name] = value
            continue
        value = getattr(ns, p.name)
        if value is not None and p.type == "table":
            value = _read_table_arg(p, value)
        if value is not None:
            given[p.name] = value
    return resolve(manifest, given, CLI_SOURCE)


def _read_table_arg(p: Param, path: str) -> list[dict[str, Any]]:
    from dawg_replay_kit.tables import TableError, convert_types, read_table

    try:
        return convert_types(read_table(path, sheet=p.sheet), p.types)
    except TableError as e:
        raise ParamsError(f"параметры не подходят ({CLI_SOURCE}):\n  `{p.name}`: {e}") from None
