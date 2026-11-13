"""config.toml: base url и лимиты. Ключ DAWG лежит в keyring, сюда не попадает."""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from dawg_replay.errors import ReplayError

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class Config:
    base_url: str = ""  # пусто: адрес по умолчанию из dawg-sdk-python
    max_parallel_runs: int = 3
    max_timeout_minutes: int = 24 * 60
    cancel_grace_seconds: int = 15
    rows_preview_limit: int = 500
    # Только для разработки: путь к packages/dawg-replay-kit, см. dawg_replay.devkit.
    dev_kit_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELDS = {f.name: f for f in fields(Config)}


def _coerce(key: str, raw: Any) -> Any:
    if key not in _FIELDS:
        raise ReplayError("unknown_config_key", f"неизвестная настройка `{key}`; есть: {', '.join(_FIELDS)}")
    want = type(getattr(Config(), key))
    if want is int:
        if isinstance(raw, bool):
            raise ReplayError("bad_config_value", f"`{key}` должен быть целым")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ReplayError("bad_config_value", f"`{key}` должен быть целым, получено {raw!r}") from None
        if value < 1:
            raise ReplayError("bad_config_value", f"`{key}` должен быть не меньше 1")
        return value
    if not isinstance(raw, str):
        raise ReplayError("bad_config_value", f"`{key}` должен быть строкой")
    if key == "base_url" and raw and not raw.startswith(("http://", "https://")):
        raise ReplayError("bad_config_value", "`base_url` должен начинаться с http:// или https://")
    return raw


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ReplayError("bad_config", f"{path}: ошибка TOML: {e}") from None
    cfg = Config()
    for key, raw in data.items():
        setattr(cfg, key, _coerce(key, raw))
    return cfg


def _toml_value(v: Any) -> str:
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(v)


coerce = _coerce  # проверка и приведение значения настройки для внешних модулей


def save_config(path: Path, cfg: Config) -> None:
    lines = [f"{k} = {_toml_value(v)}" for k, v in cfg.to_dict().items()]
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def set_config_value(path: Path, key: str, raw: str) -> Config:
    cfg = load_config(path)
    setattr(cfg, key, _coerce(key, raw))
    save_config(path, cfg)
    return cfg
