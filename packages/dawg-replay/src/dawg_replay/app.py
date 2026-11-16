"""Ядро приложения: одна точка входа для CLI и веб-интерфейса."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from dawg_replay import account, logins, tg_rules
from dawg_replay.config import Config, coerce, load_config, set_config_value, update_config
from dawg_replay.db import connect
from dawg_replay.errors import ReplayError
from dawg_replay.paths import Paths, default_home
from dawg_replay.preset_tables import PresetTables
from dawg_replay.presets import Preset, Presets
from dawg_replay.projects import Projects
from dawg_replay.registry import Registry
from dawg_replay.runs import STATUS_RUNNING, RunStore
from dawg_replay.schedules import Backend as SchedulerBackend
from dawg_replay.schedules import Schedules
from dawg_replay.secrets import Backend, SecretStore, clean_secret


class App:
    def __init__(
        self,
        home: Path | None = None,
        keyring_backend: Backend | None = None,
        scheduler_backend: SchedulerBackend | None = None,
    ):
        self.paths = Paths(home if home is not None else default_home())
        self.paths.ensure()
        self.db: sqlite3.Connection = connect(self.paths.db)
        self.registry = Registry(self.db, self.paths)
        self.presets = Presets(self.db, PresetTables(self.paths))
        self.runs = RunStore(self.db)
        self.secrets = SecretStore(keyring_backend)
        self.projects = Projects(self.db, self)
        self._scheduler_backend = scheduler_backend
        self._schedules: Schedules | None = None

    @property
    def schedules(self) -> Schedules:
        """Регулярные запуски. Планировщик ОС создаётся при первом обращении."""
        if self._schedules is None:
            self._schedules = Schedules(self, self._scheduler_backend)
        return self._schedules

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> App:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- настройки ---

    @property
    def config(self) -> Config:
        return load_config(self.paths.config)

    def set_config(self, key: str, value: str) -> Config:
        return set_config_value(self.paths.config, key, value)

    def config_info(self) -> dict[str, Any]:
        state = self.secrets.api_key_state()
        return {
            "home": str(self.paths.home),
            "config_file": str(self.paths.config),
            "config": self.config.to_dict(),
            "api_key_set": state != "missing",
            "api_key_corrupt": state == "corrupt",
        }

    def settings_view(self) -> dict[str, Any]:
        """Настройки для экрана: ключ только подсказкой (последние 4 символа)."""
        cfg = self.config
        try:
            state = self.secrets.api_key_state()
            key = self.secrets.get_api_key() if state == "set" else None
            keyring_error = None
        except ReplayError as e:
            state, key, keyring_error = "missing", None, e.message
        return {
            "api_key_set": state != "missing",
            "api_key_corrupt": state == "corrupt",
            "api_key_hint": account.key_hint(key),
            "keyring_error": keyring_error,
            "max_parallel_runs": cfg.max_parallel_runs,
            "max_timeout_minutes": cfg.max_timeout_minutes,
            "rows_preview_limit": cfg.rows_preview_limit,
            "log_limit": cfg.log_limit,
            "base_url": cfg.base_url,
        }

    def set_api_key(self, value: str, check: bool = True) -> account.Usage | None:
        """Сохраняет ключ. С check ключ сначала проверяется на платформе, неверный не сохраняется."""
        value = clean_secret(value, "ключ DAWG", command=None)  # форма UI: подсказка про терминал не нужна
        usage = account.fetch_usage(value, self.config.base_url or None) if check else None
        self.secrets.set_api_key(value)
        return usage

    def check_api_key(self) -> account.Usage:
        key = self.secrets.get_api_key()
        if not key:
            raise ReplayError("no_api_key", "ключ DAWG не задан")
        return account.fetch_usage(key, self.config.base_url or None)

    def update_limits(self, max_parallel_runs: Any, max_timeout_minutes: Any,
                      rows_preview_limit: Any = None, log_limit: Any = None) -> Config:
        """Меняет лимиты разом: при ошибке в любом ничего не сохраняется. None: не трогать."""
        values = {"max_parallel_runs": coerce("max_parallel_runs", max_parallel_runs),
                  "max_timeout_minutes": coerce("max_timeout_minutes", max_timeout_minutes)}
        for key, raw in (("rows_preview_limit", rows_preview_limit), ("log_limit", log_limit)):
            if raw is not None:
                values[key] = coerce(key, raw)
        return update_config(self.paths.config, values)

    # --- скрипты ---

    def running_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM runs WHERE status = ?", (STATUS_RUNNING,)).fetchone()[0]

    def runs_fingerprint(self) -> str:
        """Меняется, когда меняется что-то в недавних запусках: для живого обновления экранов.

        Вид `<идущих запусков>:<хеш>`; число нужно шапке для анимации знака.
        """
        rows = self.db.execute(
            "SELECT id, status, reason, finished_at FROM runs ORDER BY created_at DESC, id DESC LIMIT 100"
        ).fetchall()
        import hashlib

        digest = hashlib.sha1(repr([tuple(r) for r in rows]).encode()).hexdigest()[:16]
        return f"{self.running_count()}:{digest}"

    def script_view(self, name: str, runs_limit: int = 20) -> dict[str, Any]:
        """Экран скрипта: описание, версии, пресеты, история запусков, код текущей версии."""
        script = self.registry.get(name)
        current = self.registry.version(name)
        try:
            code = current.path.read_text(encoding="utf-8-sig")
        except OSError as e:
            code = None
            code_error = f"файл текущей версии не читается: {e.strerror}"
        else:
            code_error = None
        return {
            "script": script.to_dict(),
            "versions": [v.to_dict() for v in reversed(self.registry.versions(name))],
            "presets": [p.name for p in self.list_presets(name)],
            "runs": [r.to_dict() for r in self.runs.list(name, runs_limit)],
            "code": code,
            "code_error": code_error,
        }

    def scripts_overview(self) -> list[dict[str, Any]]:
        """Список для экрана Скрипты: скрипт и его последний запуск."""
        out = []
        for s in self.registry.list():
            last = self.runs.list(s.name, 1)
            out.append({"script": s.to_dict(), "last_run": last[0].to_dict() if last else None})
        return out

    def show(self, name: str) -> dict[str, Any]:
        script = self.registry.get(name)
        manifest = self.registry.manifest(name)
        return {
            "script": script.to_dict(),
            "manifest": manifest.to_dict(),
            "path": str(self.registry.version(name).path),
            "versions": [v.version for v in self.registry.versions(name)],
            "presets": [p.name for p in self.presets.list(name, manifest)],
            "secrets": self.secret_status(name),
        }

    def remove_project(self, name: str) -> dict[str, Any]:
        """Удаляет проект с версиями, его расписания и правила Telegram; история запусков проекта остаётся.
        Пока идёт запуск проекта, удалять нельзя."""
        from dawg_replay import launch, project_runs, tg_rules

        self.projects.get(name)
        launch.recover_lost(self)
        active = [pr.id for pr in project_runs.running(self, name)]
        if active:
            raise ReplayError("project_running", f"проект `{name}` сейчас запущен ({', '.join(active)}). "
                                                 f"Дождитесь конца или остановите: dawg-replay project stop {active[0]}")
        schedules = self.schedules.remove_for_project(name) if self.schedules.project_all(name) else []
        tg_rules.drop(self, "project", name)
        versions = self.projects.remove(name)
        return {"name": name, "versions": [v.version for v in versions], "schedules_removed": schedules}

    def remove(self, name: str) -> dict[str, Any]:
        """Удаляет скрипт, версии, пресеты, секреты, сохранённые входы и расписания; история запусков остаётся.
        Пока идёт запуск этого скрипта, удалять нельзя: его файл и секреты ещё нужны.
        Расписания снимаются из планировщика первыми: если это не удалось, скрипт остаётся."""
        from dawg_replay import launch  # launch импортирует app

        self.registry.get(name)
        launch.recover_lost(self)  # «идёт» у запусков с пропавшим супервизором закрывается
        active = [r.id for r in self.runs.running() if r.script_name == name and not r.draft]
        if active:
            raise ReplayError(
                "script_running",
                f"скрипт `{name}` сейчас запущен ({', '.join(active)}). Дождитесь конца или отмените запуск, потом удаляйте",
                {"runs": active},
            )
        secret_params = set()
        for v in self.registry.versions(name):
            try:
                m = self.registry.manifest(name, v.version)
            except ReplayError:
                continue
            secret_params |= {p.name for p in m.params if p.type == "secret"}
        schedules = self.schedules.remove_for(name) if self.schedules.all(name) else []
        versions = self.registry.remove(name)
        for param in sorted(secret_params):
            self.secrets.delete_secret(name, param)
        logins_deleted = logins.remove_dir(self.paths, name)
        PresetTables(self.paths).remove_script(name)  # секретные таблицы пресетов
        tg_rules.drop(self, "script", name)  # свои правила уведомлений в Telegram
        return {
            "name": name,
            "versions": [v.version for v in versions],
            "secrets_deleted": sorted(secret_params),
            "schedules_removed": schedules,
            "logins_deleted": logins_deleted,
            "in_projects": self.projects.using(name),
        }

    # --- сохранённые входы ---

    def list_logins(self, script: str) -> list[dict[str, Any]]:
        self.registry.get(script)
        return logins.list_logins(self.paths, script)

    def forget_login(self, script: str, name: str) -> None:
        self.registry.get(script)
        logins.forget(self.paths, script, name)

    def forget_logins(self, script: str) -> int:
        self.registry.get(script)
        return logins.forget_all(self.paths, script)

    # --- пресеты ---

    def save_preset(self, script: str, preset: str, values: dict[str, Any], files: dict[str, str] | None = None) -> Preset:
        return self.presets.save(script, preset, self.registry.manifest(script), values, files)

    def get_preset(self, script: str, preset: str) -> Preset:
        return self.presets.get(script, preset, self.registry.manifest(script))

    def list_presets(self, script: str) -> list[Preset]:
        return self.presets.list(script, self.registry.manifest(script))

    def delete_preset(self, script: str, preset: str) -> list[str]:
        """Удаляет пресет и снимает его расписания. Возвращает id снятых расписаний."""
        self.registry.get(script)
        preset = self.presets.get(script, preset, self.registry.manifest(script)).name
        removed = self.schedules.remove_for(script, preset) if self.schedules.all(script) else []
        self.presets.delete(script, preset)
        return removed

    # --- секреты ---

    def set_secret(self, script: str, param: str, value: str) -> None:
        manifest = self.registry.manifest(script)
        secret_names = [p.name for p in manifest.params if p.type == "secret"]
        p = next((x for x in manifest.params if x.name == param), None)
        if p is None or p.type != "secret":
            have = ", ".join(secret_names) or "нет"
            what = "не секрет" if p is not None else "нет в манифесте"
            raise ReplayError(
                "not_a_secret", f"параметр `{param}` {what}; секреты скрипта `{script}`: {have}"
            )
        self.secrets.set_secret(script, param, value)

    def secret_status(self, script: str) -> dict[str, bool]:
        manifest = self.registry.manifest(script)
        return {p.name: self.secrets.get_secret(script, p.name) is not None for p in manifest.params if p.type == "secret"}
