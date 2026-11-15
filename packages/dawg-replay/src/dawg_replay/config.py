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
    # Сколько плановый запуск (и run --wait) ждёт свободного слота, потом пропускается.
    wait_slot_minutes: int = 30
    rows_preview_limit: int = 500  # строк превью на таблицу на экране запуска
    log_limit: int = 2000  # последних записей журнала на экране запуска
    # Только для разработки: путь к packages/dawg-replay-kit, см. dawg_replay.devkit.
    dev_kit_source: str = ""
    # Telegram (dawg_replay.telegram): привязанный чат, имя бота и адрес Bot API. Токен в keyring.
    telegram_chat_id: str = ""
    telegram_chat_name: str = ""
    telegram_bot: str = ""
    telegram_api_url: str = ""  # пусто: https://api.telegram.org

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELDS = {f.name: f for f in fields(Config)}

# Границы целых настроек: (min, max, что это). Больше max страница запуска тормозит, меньше min бесполезно.
BOUNDS: dict[str, tuple[int, int, str]] = {
    "rows_preview_limit": (10, 10000, "строк превью на таблицу"),
    "log_limit": (100, 20000, "записей журнала на экране запуска"),
}


def _coerce(key: str, raw: Any, strict: bool = True) -> Any:
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
        if key in BOUNDS:
            lo, hi, what = BOUNDS[key]
            if not strict:
                return min(max(value, lo), hi)  # старый config.toml с прежними границами не ломает запуск
            if not lo <= value <= hi:
                raise ReplayError("bad_config_value", f"`{key}` ({what}): от {lo} до {hi}, получено {value}")
        return value
    if not isinstance(raw, str):
        raise ReplayError("bad_config_value", f"`{key}` должен быть строкой")
    if key in ("base_url", "telegram_api_url") and raw and not raw.startswith(("http://", "https://")):
        raise ReplayError("bad_config_value", f"`{key}` должен начинаться с http:// или https://")
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
        setattr(cfg, key, _coerce(key, raw, strict=False))
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
