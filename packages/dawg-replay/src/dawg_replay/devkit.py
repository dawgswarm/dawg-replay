"""Локальный dawg-replay-kit для разработки, пока kit не опубликован на PyPI.

Включается настройкой `dev_kit_source` (путь к каталогу packages/dawg-replay-kit).
Без неё приложение ведёт себя как у клиента: `uv run script.py` ставит kit с PyPI.

С настройкой приложение собирает wheel kit в `<home>/dev-kit/<хеш исходников>/`
(пересобирает только при изменении исходников) и запускает скрипт так:

    UV_FIND_LINKS=<каталог с wheel> uv run --reinstall-package dawg-replay-kit script.py

`UV_FIND_LINKS` даёт uv второй источник пакетов рядом с PyPI. `--reinstall-package`
нужен, потому что версия kit при правках не меняется, а uv кеширует окружение
скрипта по требованиям и без него продолжает брать старый kit. `UV_OVERRIDE`
для скриптов с inline-метаданными uv не применяет, поэтому он не подходит.

Сам скрипт под машину не правится: в нём остаётся `dawg-replay-kit>=1,<2`.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dawg_replay.errors import ReplayError

PACKAGE = "dawg-replay-kit"
ENV_FIND_LINKS = "UV_FIND_LINKS"
_SKIP_DIRS = {"__pycache__", ".pytest_cache", "tests", "dist", "build", ".venv"}


@dataclass(frozen=True)
class DevKit:
    find_links: Path  # каталог с wheel

    @property
    def env(self) -> dict[str, str]:
        return {ENV_FIND_LINKS: str(self.find_links)}

    @property
    def uv_args(self) -> list[str]:
        return ["--reinstall-package", PACKAGE]


def source_hash(source: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(source.rglob("*")):
        rel = f.relative_to(source)
        if not f.is_file() or any(part in _SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if f.suffix in (".pyc", ".pyo"):
            continue
        h.update(rel.as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def ensure(source: str | Path, cache: Path) -> DevKit:
    """Собирает wheel kit из `source`, если исходники изменились с прошлой сборки."""
    src = Path(source).expanduser().resolve()
    if not (src / "pyproject.toml").is_file() or not (src / "src" / "dawg_replay_kit").is_dir():
        raise ReplayError(
            "bad_dev_kit_source",
            f"dev_kit_source={source}: это не каталог dawg-replay-kit (нужны pyproject.toml и src/dawg_replay_kit)",
        )
    digest = source_hash(src)
    target = cache / digest
    if any(target.glob("*.whl")):
        return DevKit(target)
    cache.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".build-", dir=cache))
    try:
        p = subprocess.run(
            ["uv", "build", "--quiet", "--wheel", str(src), "-o", str(tmp)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if p.returncode != 0 or not any(tmp.glob("*.whl")):
            raise ReplayError("dev_kit_build_failed", f"сборка kit из {src} не удалась:\n{p.stderr.strip()}")
        try:
            tmp.rename(target)  # атомарно; параллельная сборка того же хеша уже могла успеть
        except OSError:
            if not any(target.glob("*.whl")):
                raise
    except FileNotFoundError:
        raise ReplayError("uv_not_found", "для dev_kit_source нужен uv в PATH") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # Старые сборки не нужны.
    for d in cache.iterdir():
        if d.is_dir() and d.name != digest and not d.name.startswith(".build-"):
            shutil.rmtree(d, ignore_errors=True)
    return DevKit(target)
