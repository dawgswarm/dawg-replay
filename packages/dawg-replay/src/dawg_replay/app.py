"""Ядро приложения: одна точка входа для CLI и веб-интерфейса."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from dawg_replay import account
from dawg_replay.config import Config, coerce, load_config, save_config, set_config_value
from dawg_replay.db import connect
from dawg_replay.errors import ReplayError
from dawg_replay.paths import Paths, default_home
from dawg_replay.presets import Preset, Presets
from dawg_replay.registry import Registry
from dawg_replay.runs import RunStore
from dawg_replay.secrets import Backend, SecretStore


class App:
    def __init__(self, home: Path | None = None, keyring_backend: Backend | None = None):
        self.paths = Paths(home if home is not None else default_home())
        self.paths.ensure()
        self.db: sqlite3.Connection = connect(self.paths.db)
        self.registry = Registry(self.db, self.paths)
        self.presets = Presets(self.db)
        self.runs = RunStore(self.db)
        self.secrets = SecretStore(keyring_backend)

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
        return {
            "home": str(self.paths.home),
            "config_file": str(self.paths.config),
            "config": self.config.to_dict(),
            "api_key_set": self.secrets.get_api_key() is not None,
        }

    def settings_view(self) -> dict[str, Any]:
        """Настройки для экрана: ключ только подсказкой (последние 4 символа)."""
        cfg = self.config
        try:
            key = self.secrets.get_api_key()
            keyring_error = None
        except ReplayError as e:
            key, keyring_error = None, e.message
        return {
            "api_key_set": key is not None,
            "api_key_hint": account.key_hint(key),
            "keyring_error": keyring_error,
            "max_parallel_runs": cfg.max_parallel_runs,
            "max_timeout_minutes": cfg.max_timeout_minutes,
            "base_url": cfg.base_url,
        }

    def set_api_key(self, value: str, check: bool = True) -> account.Usage | None:
        """Сохраняет ключ. С check ключ сначала проверяется на платформе, неверный не сохраняется."""
        value = value.strip()
        if not value:
            raise ReplayError("empty_secret", "ключ DAWG пустой")
        usage = account.fetch_usage(value, self.config.base_url or None) if check else None
        self.secrets.set_api_key(value)
        return usage

    def check_api_key(self) -> account.Usage:
        key = self.secrets.get_api_key()
        if not key:
            raise ReplayError("no_api_key", "ключ DAWG не задан")
        return account.fetch_usage(key, self.config.base_url or None)

    def update_limits(self, max_parallel_runs: Any, max_timeout_minutes: Any) -> Config:
        """Меняет оба лимита разом: при ошибке в любом ничего не сохраняется."""
        cfg = self.config
        cfg.max_parallel_runs = coerce("max_parallel_runs", max_parallel_runs)
        cfg.max_timeout_minutes = coerce("max_timeout_minutes", max_timeout_minutes)
        save_config(self.paths.config, cfg)
        return cfg

    # --- скрипты ---

    def runs_fingerprint(self) -> str:
        """Меняется, когда меняется что-то в недавних запусках: для живого обновления экранов."""
        rows = self.db.execute(
            "SELECT id, status, reason, finished_at FROM runs ORDER BY created_at DESC, id DESC LIMIT 100"
        ).fetchall()
        import hashlib

        return hashlib.sha1(repr([tuple(r) for r in rows]).encode()).hexdigest()[:16]

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

    def remove(self, name: str) -> dict[str, Any]:
        secret_params = set()
        for v in self.registry.versions(name):
            try:
                m = self.registry.manifest(name, v.version)
            except ReplayError:
                continue
            secret_params |= {p.name for p in m.params if p.type == "secret"}
        versions = self.registry.remove(name)
        for param in sorted(secret_params):
            self.secrets.delete_secret(name, param)
        return {"name": name, "versions": [v.version for v in versions], "secrets_deleted": sorted(secret_params)}

    # --- пресеты ---

    def save_preset(self, script: str, preset: str, values: dict[str, Any]) -> Preset:
        return self.presets.save(script, preset, self.registry.manifest(script), values)

    def get_preset(self, script: str, preset: str) -> Preset:
        return self.presets.get(script, preset, self.registry.manifest(script))

    def list_presets(self, script: str) -> list[Preset]:
        return self.presets.list(script, self.registry.manifest(script))

    def delete_preset(self, script: str, preset: str) -> None:
        self.registry.get(script)
        self.presets.delete(script, preset)

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
