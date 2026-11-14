"""Проверка собранных пакетов перед публикацией.

Ставит wheel из каталога сборки в чистое окружение и проверяет:
- `dawg-replay --version --json` отвечает и знает контракт 1.1;
- `dawg_replay_kit` импортируется;
- пример `examples/ozon-search-cards` проходит `validate`;
- скрипт с зависимостью `dawg-replay-kit` находит kit через `UV_FIND_LINKS`
  (так же он найдёт его на PyPI после публикации) и заканчивается `ok`.

    uv build --all-packages -o dist
    uv run --no-project python testing/smoke_dist.py dist

Нужен `uv` в PATH. Сеть нужна для зависимостей с PyPI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ozon-search-cards" / "script.py"

SCRIPT = """\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.1,<2"]
# ///

# /// dawg-script
# contract = "1.1"
# name = "smoke"
# title = "Проверка сборки"
# version = 1
# ///
import dawg_replay_kit as kit

kit.log.info(f"kit {kit.__version__}")
kit.result("ok", "kit найден")
"""


def run(argv: list[str], **kw) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(argv), flush=True)
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if p.returncode != 0:
        print(p.stdout, p.stderr, sep="\n", file=sys.stderr)
        raise SystemExit(f"команда завершилась с кодом {p.returncode}")
    return p


def main(dist: Path) -> None:
    dist = dist.resolve()
    wheels = sorted(p.name for p in dist.glob("*.whl"))
    if not any(w.startswith("dawg_replay-") for w in wheels) or not any(w.startswith("dawg_replay_kit-") for w in wheels):
        raise SystemExit(f"в {dist} нет wheel обоих пакетов: {wheels}")

    with tempfile.TemporaryDirectory(prefix="dawg-replay-smoke-") as tmp:
        tmp_path = Path(tmp)
        venv = tmp_path / "venv"
        run(["uv", "venv", "--quiet", str(venv)])
        bindir = venv / ("Scripts" if sys.platform == "win32" else "bin")
        python = bindir / ("python.exe" if sys.platform == "win32" else "python")
        cli = bindir / ("dawg-replay.exe" if sys.platform == "win32" else "dawg-replay")
        env = {**os.environ, "VIRTUAL_ENV": str(venv)}
        env.pop("UV_PROJECT_ENVIRONMENT", None)
        run(["uv", "pip", "install", "--quiet", "--find-links", str(dist), "dawg-replay"], env=env)

        info = json.loads(run([str(cli), "--version", "--json"]).stdout)
        if "1.1" not in info.get("contract_versions", []):
            raise SystemExit(f"неожиданный ответ --version: {info}")
        print("версия:", info["version"])

        kit = run([str(python), "-c", "import dawg_replay_kit as k; print(k.__version__)"]).stdout.strip()
        print("kit:", kit)

        run([str(cli), "validate", str(EXAMPLE)])

        script = tmp_path / "script.py"
        script.write_text(SCRIPT, encoding="utf-8")
        run_env = {
            **os.environ,
            "DAWG_REPLAY_HOME": str(tmp_path / "home"),
            "UV_FIND_LINKS": str(dist),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
        out = json.loads(run([str(cli), "run", str(script), "--json", "--follow"], env=run_env).stdout)
        status = out.get("run", {}).get("status")
        if status != "ok":
            raise SystemExit(f"скрипт закончился {status!r}: {out}")
        print("скрипт:", out["run"]["summary"])
    print("сборка в порядке")


if __name__ == "__main__":
    # На Windows вывод в канал CI идёт в кодировке локали (cp1252), кириллица в print его роняет.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if len(sys.argv) != 2:
        raise SystemExit("использование: smoke_dist.py <каталог со сборкой>")
    main(Path(sys.argv[1]))
