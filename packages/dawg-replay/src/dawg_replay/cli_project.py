"""Команды `dawg-replay project`: файл проекта, проверка, версии, экспорт."""

from __future__ import annotations

import argparse
from pathlib import Path

from dawg_replay import projects as P
from dawg_replay.app import App
from dawg_replay.cli_store import Handler, run_handler
from dawg_replay.errors import ReplayError

WHEN_WORDS = {"partial": "готово или частично", "ok": "только готово", "any": "любой исход"}
WAIT_WORDS = {"all": "после всех входящих", "any": "после любой входящей"}


def _problems(problems: list[P.Problem]) -> list[str]:
    return [str(p) for p in problems]


def _saved(r: P.SaveResult) -> tuple[dict, str]:
    name, v = r.info.name, r.version.version
    if r.status == "unchanged":
        text = f"{name} v{v}: то же содержимое, ничего не изменилось"
    elif r.previous_version is None:
        text = f"проект добавлен: {name} v{v}, {r.info.title}"
    else:
        text = f"{name}: новая версия v{v} (была v{r.previous_version}), она теперь текущая"
    lines = [text]
    lines += [f"создан пресет «{x['preset']}» скрипта {x['script']} (узел {x['node']})" for x in r.presets_created]
    lines += [f"пресет «{x['preset']}» скрипта {x['script']} уже есть, не перезаписан" for x in r.presets_kept]
    lines += _problems(r.warnings)
    return r.to_dict(), "\n".join(lines)


def _add(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    return _saved(app.projects.add(args.file))


def _update(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    return _saved(app.projects.update(args.file))


def _list(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    items = app.projects.list()
    text = "\n".join(f"{x.name}  v{x.current_version}  {x.title}" for x in items) or \
        "проектов нет; добавить: dawg-replay project add <имя>.project.toml"
    return {"projects": [x.to_dict() for x in items]}, text


def _edge_line(e: P.Edge) -> str:
    parts = [f"{e.source} → {e.target}", WHEN_WORDS[e.when]]
    if e.if_file:
        parts.append(f"если есть {e.if_file}")
    if e.files:
        parts.append(", ".join(f.label for f in e.files))
    if e.source_column:
        parts.append(f"колонка источника «{e.source_column}»")
    return "; ".join(parts)


def _show(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    s = app.projects.load(args.name, args.project_version)
    pr = s.project
    problems = app.projects.check(pr)
    lines = [f"{pr.name} v{s.version.version}{'' if s.version.current else ' (не текущая)'}: {pr.title}"]
    if pr.description:
        lines.append(pr.description)
    if pr.timeout_minutes:
        lines.append(f"таймаут: {pr.timeout_minutes} мин")
    lines.append(f"версии: {s.info.versions_count}, текущая v{s.info.current_version}")
    lines.append("узлы:")
    for n in pr.nodes:
        extra = []
        if len(pr.incoming(n.id)) > 1:
            extra.append(WAIT_WORDS[n.wait])
        if n.values:
            extra.append("свои значения: " + ", ".join(n.values))
        tail = f"  [{'; '.join(extra)}]" if extra else ""
        lines.append(f"  {n.id}  {app.projects.node_title(n)}  ({n.script}"
                     f"{', пресет «' + n.preset + '»' if n.preset else ', без пресета'}){tail}")
    if pr.edges:
        lines.append("стрелки:")
        lines += [f"  {_edge_line(e)}" for e in pr.edges]
    lines += _problems(problems)
    data = s.to_dict() | {"problems": [p.to_dict() for p in problems]}
    return data, "\n".join(lines)


def _export(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    ex = app.projects.export(args.name, args.project_version, preset_values=not args.no_preset_values)
    data = ex.to_dict()
    if args.output:
        out = Path(args.output)
        try:
            out.write_text(ex.text, encoding="utf-8")
        except OSError as e:
            raise ReplayError("file_unwritable", f"не удалось записать {out}: {e.strerror or e}") from None
        data["file"] = str(out)
        text = "\n".join([f"проект записан в {out}", *ex.warnings])
    else:
        text = ex.text.rstrip("\n") + ("\n" + "\n".join(f"# {w}" for w in ex.warnings) if ex.warnings else "")
    return data, text


def _check(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    project, problems = app.projects.check_source(args.source)
    data = {"name": project.name, "problems": [p.to_dict() for p in problems]}
    if any(p.level == "error" for p in problems):
        raise ReplayError("invalid_project", f"{args.source}: проект с ошибками:\n" +
                          "\n".join(f"  {x}" for x in _problems(problems)), data["problems"])
    text = "\n".join(_problems(problems)) or \
        f"{project.name}: узлов {len(project.nodes)}, стрелок {len(project.edges)}, ошибок нет"
    return data, text


def _versions(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    vs = app.projects.versions(args.name)
    where = {"file": "из файла", "editor": "с холста"}
    text = "\n".join(f"{'*' if v.current else ' '} v{v.version}  {v.added_at}  {where.get(v.origin, v.origin)}  {v.title}"
                     for v in vs)
    return {"name": args.name, "versions": [v.to_dict() for v in vs]}, text


def _rollback(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    v = app.projects.rollback(args.name, args.target_version)
    return {"version": v.to_dict()}, f"{args.name}: текущая версия теперь v{v.version}"


def _remove(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    if not args.yes:
        versions = [v.version for v in app.projects.versions(args.name)]
        raise ReplayError("confirmation_required",
                          f"будут удалены проект `{args.name}` и его версии {versions}. "
                          "Скрипты и пресеты останутся. Повторите с --yes")
    r = app.remove_project(args.name)
    return r, f"проект `{args.name}` удалён" + (f", расписания сняты: {', '.join(r['schedules_removed'])}"
                                                if r["schedules_removed"] else "")


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    def add(name: str, handler: Handler, help: str, description: str | None = None) -> argparse.ArgumentParser:
        p = g.add_parser(name, parents=[common], help=help, description=description or help)
        p.set_defaults(func=lambda args, h=handler: run_handler(h, args))
        return p

    g = sub.add_parser(
        "project",
        parents=[common],
        help="проекты: схемы из скриптов",
        description="Проект это схема из узлов (скрипт + пресет) и стрелок между ними, файл <имя>.project.toml.",
    ).add_subparsers(dest="project_command", metavar="<действие>", required=True)

    p = add("add", _add, "добавить проект из файла",
            "Проверяет файл и сохраняет первую версию. Недостающие пресеты создаются из preset_values, "
            "существующие не перезаписываются.")
    p.add_argument("file", help="файл <имя>.project.toml")
    p = add("update", _update, "новая версия проекта из файла")
    p.add_argument("file", help="файл <имя>.project.toml")
    add("list", _list, "проекты")
    p = add("show", _show, "узлы, стрелки и проверка проекта")
    p.add_argument("name", help="имя проекта")
    p.add_argument("--version", dest="project_version", type=int, metavar="N", help="версия (по умолчанию текущая)")
    p = add("export", _export, "файл проекта",
            "Текст версии проекта. По умолчанию с несекретными значениями пресетов узлов, "
            "чтобы перенести проект на другую машину.")
    p.add_argument("name", help="имя проекта")
    p.add_argument("-o", "--output", metavar="ФАЙЛ", help="записать в файл, а не на экран")
    p.add_argument("--version", dest="project_version", type=int, metavar="N", help="версия (по умолчанию текущая)")
    p.add_argument("--no-preset-values", action="store_true", help="без значений пресетов: текст версии как сохранён")
    p = add("check", _check, "проверить файл или проект без сохранения")
    p.add_argument("source", metavar="ФАЙЛ|ИМЯ", help="файл проекта или имя сохранённого")
    p = add("versions", _versions, "версии проекта")
    p.add_argument("name", help="имя проекта")
    p = add("rollback", _rollback, "сделать текущей прошлую версию")
    p.add_argument("name", help="имя проекта")
    p.add_argument("target_version", type=int, metavar="N", help="номер версии")
    p = add("remove", _remove, "удалить проект и его версии")
    p.add_argument("name", help="имя проекта")
    p.add_argument("--yes", action="store_true", help="подтвердить удаление")

    from dawg_replay import cli_project_run

    cli_project_run.register(g, common)
