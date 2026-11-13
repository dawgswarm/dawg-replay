"""Ядро приложения: одна точка входа для CLI и веб-интерфейса."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from dawg_replay.config import Config, load_config, set_config_value
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

    # --- скрипты ---

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
