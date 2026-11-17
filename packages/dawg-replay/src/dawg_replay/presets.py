"""Пресеты: именованные наборы значений параметров.

Секреты (`secret`) в пресеты не попадают, они в хранилище паролей. Секретная таблица
(`table` с `secret = true`) хранится отдельным файлом (dawg_replay.preset_tables), в БД
у пресета только ссылка, имя файла и число строк."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from dawg_replay_kit.manifest import Manifest, check_value

from dawg_replay.db import utcnow
from dawg_replay.errors import ReplayError
from dawg_replay.preset_tables import PresetTables
from dawg_replay.values import check_partial, filter_explained

NAME_MAX = 64
# Имена файлов таблиц хранятся рядом со значениями. Имя параметра не начинается с `_`,
# поэтому ключ ни с чем не совпадёт.
FILES_KEY = "_files"
FILE_PARAMS_KEY = "_file_params"  # параметр file -> [{"ref", "name", "size"}]; сами файлы в preset-tables/
SECRET_KEY = "_secret_tables"  # параметр -> {"ref", "file", "count"}; сама таблица в preset-tables/
REFS_KEY = "_results"  # параметр table/file -> ссылка на результат (run:… или last:…, dawg_replay.transfer)


def normalize(name: str | None) -> str:
    """Пробелы по краям убираются, неразрывный пробел из копипасты становится обычным."""
    return (name or "").replace("\u00a0", " ").replace("\u202f", " ").strip()


def secret_tables_note(manifest: Manifest, preset: Preset) -> str | None:
    """Что сказать при сохранении пресета про секретные таблицы скрипта."""
    from dawg_replay.values import rows_word

    notes = []
    for p in manifest.params:
        if p.type == "table" and "preset" not in p.inputs:
            notes.append(f"Таблица «{p.title}» в пресет не сохраняется: скрипт берёт её только при запуске.")
            continue
        if p.type != "table" or not p.secret:
            continue
        t = preset.secret_tables.get(p.name)
        if t:
            notes.append(f"Таблица «{p.title}» ({t['file']}, {rows_word(t['count'])}) сохранена в пресете, видна только вам.")
        elif p.required:
            notes.append(f"Таблица «{p.title}» не загружена: регулярный запуск по этому пресету не начнётся.")
    return " ".join(notes) or None


def clean_name(name: str) -> str:
    """Имя пресета без пробелов по краям. Любые печатные символы, включая пробелы и
    кириллицу. Нельзя: пустое, длиннее 64, управляющие символы, `-` в начале
    (в `run --preset -x` такое имя примут за флаг)."""
    name = normalize(name)
    if not name:
        raise ReplayError("bad_preset_name", "имя пресета пустое")
    if len(name) > NAME_MAX:
        raise ReplayError("bad_preset_name", f"имя пресета длиннее {NAME_MAX} символов")
    if not name.isprintable():
        raise ReplayError("bad_preset_name", "в имени пресета есть перевод строки, табуляция или другой непечатный символ")
    if name.startswith("-"):
        raise ReplayError("bad_preset_name", "имя пресета не может начинаться с «-»")
    return name


@dataclass(frozen=True)
class Preset:
    script: str
    name: str
    params: dict[str, Any]  # только то, что подходит текущей версии
    dropped: list[str]  # ключи, которые текущая версия не принимает
    created_at: str
    updated_at: str
    files: dict[str, str] = field(default_factory=dict)  # параметр table -> имя исходного файла
    dropped_why: dict[str, str] = field(default_factory=dict)  # отброшенное -> почему
    secret_tables: dict[str, dict[str, Any]] = field(default_factory=dict)  # параметр -> {"ref", "file", "count"}
    missing: dict[str, str] = field(default_factory=dict)  # обязательные без значения в пресете: имя -> подпись
    file_params: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # file -> [{"ref", "name", "size"}]
    results: dict[str, str] = field(default_factory=dict)  # table/file -> run:… или last:… (результат другого скрипта)

    def outdated(self) -> str | None:
        """Текст для человека, если пресет не подходит текущей версии скрипта: значения, которые она
        не принимает, и обязательные значения, которых в пресете нет."""
        parts = []
        if self.dropped:
            parts.append("текущая версия не принимает: " + "; ".join(self.dropped_why.get(n, f"`{n}`") for n in self.dropped))
        if self.missing:
            titles = ", ".join(f"«{t}»" for t in self.missing.values())
            parts.append(("нет значения " if len(self.missing) == 1 else "нет значений ") + titles)
        if not parts:
            return None
        return (f"пресет «{self.name}» сохранён для другой версии скрипта: {'; '.join(parts)}. "
                "Откройте пресет в форме запуска, поправьте значения и сохраните его заново")

    def to_dict(self) -> dict[str, Any]:
        return {
            "script": self.script,
            "name": self.name,
            "params": self.params,
            "dropped": self.dropped,
            "dropped_why": self.dropped_why,
            "missing": list(self.missing),
            "files": self.files,
            "secret_tables": {k: {"file": v["file"], "count": v["count"]} for k, v in self.secret_tables.items()},
            "file_params": {k: [{"name": f["name"], "size": f["size"]} for f in v] for k, v in self.file_params.items()},
            "results": self.results,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Presets:
    """Значения хранятся как сохранены. При чтении они фильтруются по манифесту
    текущей версии, поэтому откат версии не теряет данные пресета."""

    def __init__(self, conn: sqlite3.Connection, tables: PresetTables | None = None):
        self._db = conn
        self._tables = tables

    def save(
        self, script: str, name: str, manifest: Manifest, values: dict[str, Any], files: dict[str, str] | None = None
    ) -> Preset:
        """`files`: имена файлов таблиц. Секретная таблица пишется в отдельный файл, пресет хранит ссылку.
        Пресет с тем же именем перезаписывается целиком, прежние файлы таблиц удаляются."""
        name = clean_name(name)
        from dawg_replay.transfer import param_ref

        # Ссылка на результат другого скрипта хранится строкой, файл или значение подставит запуск.
        by_name = {p.name: p for p in manifest.params}
        results = {k: v for k, v in values.items() if k in by_name and param_ref(by_name[k], v) is not None}
        values = {k: v for k, v in values.items() if k not in results}
        # Таблица, которую скрипт не берёт из пресета (input без "preset"), в пресет не пишется.
        no_preset = {p.name for p in manifest.params if p.type == "table" and "preset" not in p.inputs}
        values = {k: v for k, v in values.items() if k not in no_preset}
        secret = {p.name: p for p in manifest.params if p.type == "table" and p.secret}
        file_params = {p.name: p for p in manifest.params if p.type == "file"}
        tables = {k: v for k, v in values.items() if k in secret}
        file_vals = {k: v for k, v in values.items() if k in file_params and v not in (None, [])}
        values = {k: v for k, v in values.items() if k not in secret and k not in file_params}
        files = files or {}
        problems = check_partial(manifest, values, allow_secrets=False)
        problems += [f"`{k}`: {pr}" for k, v in tables.items() if (pr := check_value(secret[k], v))]
        problems += [f"`{k}`: {pr}" for k, v in file_vals.items() if (pr := check_value(file_params[k], v))]
        if problems:
            raise ReplayError(
                "bad_params", "значения не подходят:\n" + "\n".join(f"  {x}" for x in problems), problems
            )
        if (tables or file_vals) and self._tables is None:
            raise ReplayError("internal", "хранилище секретных таблиц пресетов не подключено")
        old = self._stored_refs(script, name)
        meta = {k: {"ref": self._tables.put(script, v), "file": files.get(k) or "таблица", "count": len(v)}
                for k, v in tables.items()} if tables else {}
        file_meta = {k: [{"ref": self._tables.put_file(script, f["path"]), "name": f["name"], "size": f["size"]}
                         for f in (v if isinstance(v, list) else [v])] for k, v in file_vals.items()}
        files = {k: v for k, v in files.items() if k in values}
        stored: dict[str, Any] = dict(values)
        if file_meta:
            stored[FILE_PARAMS_KEY] = file_meta
        if files:
            stored[FILES_KEY] = files
        if meta:
            stored[SECRET_KEY] = meta
        if results:
            stored[REFS_KEY] = results
        now = utcnow()
        self._db.execute(
            "INSERT INTO presets (script_name, name, params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (script_name, name) DO UPDATE SET params_json = excluded.params_json, updated_at = excluded.updated_at",
            (script, name, json.dumps(stored, ensure_ascii=False), now, now),
        )
        self._drop_refs(script, old)
        return self.get(script, name, manifest)

    def secret_rows(self, preset: Preset) -> dict[str, list[dict[str, Any]]]:
        """Секретные таблицы пресета для запуска. Пропавший файл: таблицы нет."""
        out = {}
        for k, t in preset.secret_tables.items():
            rows = self._tables.get(preset.script, t["ref"]) if self._tables else None
            if rows is not None:
                out[k] = rows
        return out

    def file_values(self, preset: Preset, manifest: Manifest) -> dict[str, Any]:
        """Файлы пресета для запуска и формы: {"path", "name", "size"} (список при multiple).
        Пропавший файл: параметра нет, как у незаданного."""
        multiple = {p.name: p.multiple for p in manifest.params if p.type == "file"}
        out: dict[str, Any] = {}
        for k, items in preset.file_params.items():
            recs = []
            for f in items:
                path = self._tables.file_path(preset.script, f.get("ref")) if self._tables else None
                if path is not None:
                    recs.append({"path": str(path), "name": f["name"], "size": f["size"]})
            if recs and len(recs) == len(items):
                out[k] = recs if multiple.get(k) else recs[0]
        return out

    def _stored_refs(self, script: str, name: str) -> list[str]:
        row = self._db.execute("SELECT params_json FROM presets WHERE script_name = ? AND name = ?", (script, name)).fetchone()
        if row is None:
            return []
        stored = json.loads(row["params_json"])
        meta = stored.get(SECRET_KEY)
        refs = [t.get("ref") for t in meta.values() if isinstance(t, dict)] if isinstance(meta, dict) else []
        fmeta = stored.get(FILE_PARAMS_KEY)
        if isinstance(fmeta, dict):
            refs += [f.get("ref") for v in fmeta.values() if isinstance(v, list) for f in v if isinstance(f, dict)]
        return refs

    def _drop_refs(self, script: str, refs: list[str]) -> None:
        if self._tables is not None:
            for ref in refs:
                self._tables.delete(script, ref)

    def get(self, script: str, name: str, manifest: Manifest) -> Preset:
        name = normalize(name)
        row = self._db.execute(
            "SELECT * FROM presets WHERE script_name = ? AND name = ?", (script, name)
        ).fetchone()
        if row is None:
            raise ReplayError("preset_not_found", f"у скрипта `{script}` нет пресета `{name}`")
        return self._preset(row, manifest)

    def list(self, script: str, manifest: Manifest) -> list[Preset]:
        rows = self._db.execute("SELECT * FROM presets WHERE script_name = ? ORDER BY name", (script,)).fetchall()
        return [self._preset(r, manifest) for r in rows]

    def delete(self, script: str, name: str) -> None:
        name = normalize(name)
        refs = self._stored_refs(script, name)
        cur = self._db.execute("DELETE FROM presets WHERE script_name = ? AND name = ?", (script, name))
        if cur.rowcount == 0:
            raise ReplayError("preset_not_found", f"у скрипта `{script}` нет пресета `{name}`")
        self._drop_refs(script, refs)

    @staticmethod
    def _preset(row: sqlite3.Row, manifest: Manifest) -> Preset:
        stored = json.loads(row["params_json"])
        files = stored.pop(FILES_KEY, None)
        meta = stored.pop(SECRET_KEY, None)
        fmeta = stored.pop(FILE_PARAMS_KEY, None)
        refs = stored.pop(REFS_KEY, None)
        from dawg_replay.transfer import param_ref

        by_name = {p.name: p for p in manifest.params}
        results = {k: v for k, v in (refs.items() if isinstance(refs, dict) else ())
                   if k in by_name and param_ref(by_name[k], v) is not None}
        file_now = {p.name for p in manifest.params if p.type == "file"}
        file_params = {
            k: [{"ref": f.get("ref"), "name": str(f.get("name") or "файл"), "size": int(f.get("size") or 0)}
                for f in v if isinstance(f, dict)]
            for k, v in (fmeta.items() if isinstance(fmeta, dict) else ()) if k in file_now and isinstance(v, list)
        }
        secret_now = {p.name for p in manifest.params if p.type == "table" and p.secret}
        secret_tables = {
            k: {"ref": t.get("ref"), "file": str(t.get("file") or "таблица"), "count": int(t.get("count") or 0)}
            for k, t in (meta.items() if isinstance(meta, dict) else ()) if k in secret_now and isinstance(t, dict)
        }
        kept, why = filter_explained(manifest, stored)
        dropped = list(why)
        files = {k: v for k, v in files.items() if k in kept and isinstance(v, str)} if isinstance(files, dict) else {}
        # Обязательное без значения: новая версия добавила параметр, а пресет сохранён раньше.
        # Секреты берутся из хранилища паролей, секретная таблица из файла пресета.
        missing = {
            p.name: p.title for p in manifest.params
            if p.required and p.type != "secret" and p.default is None and p.name not in kept
            and not (p.type == "table" and p.secret and p.name in secret_tables) and p.name not in why
            and not (p.type == "file" and p.name in file_params) and p.name not in results
        }
        return Preset(row["script_name"], row["name"], kept, dropped, row["created_at"], row["updated_at"], files, why,
                      secret_tables, missing, file_params, results)
