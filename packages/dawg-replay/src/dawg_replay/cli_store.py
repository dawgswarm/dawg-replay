"""Команды CLI для настроек, реестра, пресетов и секретов. Тонкая обёртка над App."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Callable
from typing import Any

from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from dawg_replay.values import parse_set_args, read_params_file

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BAD_INPUT = 2  # значение секрета с управляющими символами: не сохранено

# Обработчик получает App и аргументы, возвращает (данные для --json, текст для человека).
Handler = Callable[[App, argparse.Namespace], "tuple[dict[str, Any], str]"]


def _out_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def run_handler(handler: Handler, args: argparse.Namespace) -> int:
    try:
        with App() as app:
            data, text = handler(app, args)
    except ReplayError as e:
        if args.json:
            _out_json({"ok": False, "error": e.to_dict()})
        else:
            print(f"ошибка: {e.message}", file=sys.stderr)
        return EXIT_BAD_INPUT if e.code == "bad_secret" else EXIT_ERROR
    if args.json:
        _out_json({"ok": True, **data})
    elif text:
        print(text)
    return EXIT_OK


def _read_secret(prompt: str, command: str) -> str:
    """С терминала спрашивает без эха, иначе читает первую строку stdin (для агента и пайпов).
    Очистка и проверка значения в secrets.clean_secret."""
    if sys.stdin is not None and sys.stdin.isatty():
        print(
            f"Вставьте значение и нажмите Enter, на экране оно не появится. Если вставка не проходит: "
            f"pbpaste | {command} (macOS) или {command} < файл",
            file=sys.stderr,
        )
        return getpass.getpass(prompt)
    line = sys.stdin.readline() if sys.stdin is not None else ""
    return line.rstrip("\r\n")


# --- настройки ---


def _config_set_key(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    app.secrets.set_api_key(_read_secret("Ключ DAWG: ", "dawg-replay config set-key"))
    return {"api_key_set": True}, "ключ DAWG сохранён в системном хранилище паролей"


def _config_show(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    info = app.config_info()
    lines = [f"каталог данных: {info['home']}", f"ключ DAWG: {_key_state(info)}"]
    lines += [f"{k} = {v!r}" for k, v in info["config"].items()]
    return info, "\n".join(lines)


def _key_state(info: dict[str, Any]) -> str:
    if info["api_key_corrupt"]:
        return "задан, но повреждён (задайте заново: dawg-replay config set-key)"
    return "задан" if info["api_key_set"] else "не задан"


def _config_set(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    cfg = app.set_config(args.key, args.value)
    return {"config": cfg.to_dict()}, f"{args.key} = {getattr(cfg, args.key)!r}"


# --- скрипты ---


def _add(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    r = app.registry.add(args.file)
    s, v = r.script, r.version
    if r.status == "unchanged":
        text = f"{s.name} v{v.version} уже зарегистрирован с тем же содержимым, ничего не изменилось"
    elif r.previous_version is None:
        text = f"зарегистрирован {s.name} v{v.version}: {s.title}"
    else:
        text = f"{s.name}: новая версия v{v.version} (была v{r.previous_version}), она теперь текущая"
    return r.to_dict(), text


def _list(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    scripts = app.registry.list()
    if not scripts:
        text = "скриптов нет; зарегистрировать: dawg-replay add script.py"
    else:
        text = "\n".join(f"{s.name}  v{s.current_version}  {s.title}" for s in scripts)
    return {"scripts": [s.to_dict() for s in scripts]}, text


def _show(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    info = app.show(args.name)
    m = info["manifest"]
    lines = [f"{m['name']} v{m['version']}: {m['title']}"]
    if m.get("description"):
        lines.append(m["description"])
    lines.append(f"файл: {info['path']}")
    lines.append(f"версии: {', '.join(str(v) for v in info['versions'])}")
    lines.append(f"таймаут: {m['timeout_minutes']} мин")
    if m["params"]:
        lines.append("параметры:")
        for p in m["params"]:
            extra = []
            if p.get("required"):
                extra.append("обязательный")
            if "default" in p:
                extra.append(f"по умолчанию {p['default']!r}")
            if p["type"] == "secret":
                extra.append("задан" if info["secrets"].get(p["name"]) else "не задан")
            tail = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"  {p['name']}: {p['type']}, {p['title']}{tail}")
    lines.append(f"пресеты: {', '.join(info['presets']) or 'нет'}")
    return info, "\n".join(lines)


def _versions(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    vs = app.registry.versions(args.name)
    text = "\n".join(f"{'*' if v.current else ' '} v{v.version}  {v.added_at}  {v.title}" for v in vs)
    return {"name": args.name, "versions": [v.to_dict() for v in vs]}, text


def _rollback(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    v = app.registry.rollback(args.name, args.target_version)
    return {"version": v.to_dict()}, f"{args.name}: текущая версия теперь v{v.version}"


def _remove(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    if not args.yes:
        versions = [v.version for v in app.registry.versions(args.name)]
        raise ReplayError(
            "confirmation_required",
            f"будут удалены скрипт `{args.name}`, версии {versions}, его пресеты и секреты. "
            "История запусков останется. Повторите с --yes",
        )
    r = app.remove(args.name)
    return r, f"скрипт `{args.name}` удалён"


# --- пресеты ---


def _collect_values(app: App, script: str, args: argparse.Namespace) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if args.params:
        values.update(read_params_file(args.params))
    if args.set:
        values.update(parse_set_args(app.registry.manifest(script), args.set))
    return values


def _preset_save(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    values = _collect_values(app, args.script, args)
    p = app.save_preset(args.script, args.preset, values)
    return {"preset": p.to_dict()}, f"пресет `{p.name}` сохранён: {json.dumps(p.params, ensure_ascii=False)}"


def _preset_list(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    presets = app.list_presets(args.script)
    lines = []
    for p in presets:
        line = f"{p.name}  {json.dumps(p.params, ensure_ascii=False)}"
        if p.dropped:
            line += f"  (текущая версия не принимает: {', '.join(p.dropped)})"
        lines.append(line)
    return {"presets": [p.to_dict() for p in presets]}, "\n".join(lines) or "пресетов нет"


def _preset_delete(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    app.delete_preset(args.script, args.preset)
    return {"deleted": args.preset}, f"пресет `{args.preset}` удалён"


# --- секреты ---


def _secret_set(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    app.registry.manifest(args.script)  # понятная ошибка до ввода значения
    app.set_secret(args.script, args.param, _read_secret(f"{args.script}.{args.param}: ", f"dawg-replay secret set {args.script} {args.param}"))
    return {"script": args.script, "param": args.param}, f"секрет `{args.param}` скрипта `{args.script}` сохранён"


# --- регистрация ---


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    def add(parent: Any, name: str, handler: Handler, help: str) -> argparse.ArgumentParser:
        p = parent.add_parser(name, parents=[common], help=help, description=help)
        p.set_defaults(func=lambda args, h=handler: run_handler(h, args))
        return p

    def group(name: str, help: str) -> Any:
        p = sub.add_parser(name, parents=[common], help=help, description=help)
        g = p.add_subparsers(dest=f"{name}_command", metavar="<действие>", required=True)
        return g

    cfg = group("config", "настройки приложения")
    add(cfg, "set-key", _config_set_key, "сохранить ключ DAWG в системное хранилище паролей (ввод с терминала или из stdin: pbpaste | dawg-replay config set-key)")
    add(cfg, "show", _config_show, "показать настройки и каталог данных")
    p = add(cfg, "set", _config_set, "изменить настройку в config.toml")
    p.add_argument("key")
    p.add_argument("value")

    p = add(sub, "add", _add, "зарегистрировать скрипт или его новую версию")
    p.add_argument("file", help="путь к script.py")
    add(sub, "list", _list, "список скриптов")
    p = add(sub, "show", _show, "скрипт: манифест, версии, пресеты, секреты")
    p.add_argument("name")
    p = add(sub, "versions", _versions, "версии скрипта")
    p.add_argument("name")
    p = add(sub, "rollback", _rollback, "сделать текущей другую версию")
    p.add_argument("name")
    p.add_argument("target_version", metavar="version", type=int)
    p = add(sub, "remove", _remove, "удалить скрипт со всеми версиями, пресетами и секретами")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="подтвердить удаление")

    pre = group("preset", "пресеты параметров")
    p = add(pre, "save", _preset_save, "сохранить пресет (перезаписывает пресет с тем же именем)")
    p.add_argument("script")
    p.add_argument("preset")
    p.add_argument("--params", metavar="FILE", help="JSON со значениями")
    p.add_argument("--set", action="append", metavar="K=V", help="значение параметра, можно повторять")
    p = add(pre, "list", _preset_list, "пресеты скрипта")
    p.add_argument("script")
    p = add(pre, "delete", _preset_delete, "удалить пресет")
    p.add_argument("script")
    p.add_argument("preset")

    sec = group("secret", "секреты скриптов")
    p = add(sec, "set", _secret_set, "сохранить секрет в системное хранилище паролей (ввод с терминала или из stdin)")
    p.add_argument("script")
    p.add_argument("param")
