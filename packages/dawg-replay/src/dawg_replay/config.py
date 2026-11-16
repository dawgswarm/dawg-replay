"""config.toml: base url и лимиты. Ключ DAWG лежит в keyring, сюда не попадает."""

from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
    # Уведомления в Telegram: о чём писать (dawg_replay.tg_rules). У скрипта и расписания свои переопределения.
    telegram_failed: bool = True
    telegram_ask: bool = True
    telegram_done_scheduled: bool = True
    telegram_done_manual: str = "long"  # always, long (если шёл дольше telegram_long_minutes), never
    telegram_long_minutes: int = 10
    telegram_suspicious: bool = True
    telegram_attach_main: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_FIELDS = {f.name: f for f in fields(Config)}

# Границы целых настроек: (min, max, что это). Больше max страница запуска тормозит, меньше min бесполезно.
BOUNDS: dict[str, tuple[int, int, str]] = {
    "rows_preview_limit": (10, 10000, "строк превью на таблицу"),
    "log_limit": (100, 20000, "записей журнала на экране запуска"),
    "telegram_long_minutes": (1, 24 * 60, "минут, после которых ручной запуск считается долгим"),
}

# Строковые настройки с фиксированным набором значений.
CHOICES: dict[str, tuple[str, ...]] = {
    "telegram_done_manual": ("always", "long", "never"),
}
_TRUE = ("1", "true", "yes", "on", "да", "вкл")
_FALSE = ("0", "false", "no", "off", "нет", "выкл")


def parse_bool(key: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in _TRUE:
        return True
    if isinstance(raw, str) and raw.strip().lower() in _FALSE:
        return False
    raise ReplayError("bad_config_value", f"`{key}`: нужно on или off, получено {raw!r}")


def _coerce(key: str, raw: Any, strict: bool = True) -> Any:
    if key not in _FIELDS:
        raise ReplayError("unknown_config_key", f"неизвестная настройка `{key}`; есть: {', '.join(_FIELDS)}")
    want = type(getattr(Config(), key))
    if want is bool:
        return parse_bool(key, raw)
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
    if key in CHOICES and raw not in CHOICES[key]:
        if not strict:
            return getattr(Config(), key)
        raise ReplayError("bad_config_value", f"`{key}`: одно из {', '.join(CHOICES[key])}, получено {raw!r}")
    return raw


def load_config(path: Path) -> Config:
    try:
        # Под тем же замком, что запись: в одном процессе чтение не попадает на замену файла.
        with _THREAD_LOCK:
            text = _windows_retry(lambda: path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ReplayError("bad_config", f"{path}: ошибка TOML: {e}") from None
    cfg = Config()
    for key, raw in data.items():
        setattr(cfg, key, _coerce(key, raw, strict=False))
    return cfg


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(v)


coerce = _coerce  # проверка и приведение значения настройки для внешних модулей


# --- запись ----------------------------------------------------------------------
#
# config.toml читают и пишут разные потоки одного процесса (запрос UI, поток привязки
# Telegram, запуски) и разные процессы (CLI рядом с открытым UI). На Windows файл,
# который сейчас заменяют (os.replace), коротко не открывается (PermissionError), а
# заменить файл, открытый другим на чтение, тоже нельзя. Поэтому: временный файл со
# своим именем у каждого писателя, чтение и запись в одном процессе под общим замком,
# изменение «прочитать, поменять, записать» под блокировкой и между процессами
# (update_config), а для другого процесса повтор чтения и замены при PermissionError на Windows.

_IS_WINDOWS = sys.platform == "win32"
_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.1, 0.2, 0.2, 0.3, 0.5)  # около 1.5 с всего
_THREAD_LOCK = threading.RLock()


def _windows_retry(fn):
    for delay in _RETRY_DELAYS:
        try:
            return fn()
        except PermissionError:
            if not _IS_WINDOWS:
                raise
            time.sleep(delay)
    return fn()


if _IS_WINDOWS:  # pragma: no cover - проверяется на Windows
    import msvcrt

    def _lock_file(f) -> None:
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # сам ждёт около 10 с
                return
            except OSError:
                continue

    def _unlock_file(f) -> None:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl

    def _lock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def config_lock(path: Path) -> Iterator[None]:
    """Исключительное изменение config.toml: между потоками и между процессами."""
    with _THREAD_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a+b") as f:
            _lock_file(f)
            try:
                yield
            finally:
                _unlock_file(f)


def save_config(path: Path, cfg: Config) -> None:
    lines = [f"{k} = {_toml_value(v)}" for k, v in cfg.to_dict().items()]
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with _THREAD_LOCK:
        _save_locked(path, tmp, lines)


def _save_locked(path: Path, tmp: Path, lines: list[str]) -> None:
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _windows_retry(lambda: os.replace(tmp, path))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def update_config(path: Path, values: dict[str, Any]) -> Config:
    """Меняет ключи поверх текущего файла под блокировкой: параллельные изменения не теряются."""
    with config_lock(path):
        cfg = load_config(path)
        for k, v in values.items():
            setattr(cfg, k, v)
        save_config(path, cfg)
        return cfg


def set_config_value(path: Path, key: str, raw: str) -> Config:
    return update_config(path, {key: _coerce(key, raw)})
