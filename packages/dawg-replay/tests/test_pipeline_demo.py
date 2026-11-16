"""Демо-скрипты examples/pipeline-demo: передача файла результата ссылкой last: и проверка колонок.

Без браузера, сети, системного планировщика и системного хранилища паролей: скрипты запускаются интерпретатором
тестов, дом приложения временный.
"""

import csv
import json
import sys
from pathlib import Path

import pytest

from dawg_replay import launch

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "examples" / "pipeline-demo"
SCRIPTS = ["demo-wb-prices", "demo-ozon-prices", "demo-update-prices", "demo-report", "demo-ping", "demo-pong"]


@pytest.fixture
def demo(cli, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    for name in SCRIPTS:
        code, data = cli("add", str(DEMO / f"{name}.py"))
        assert code == 0, data
    return cli


def test_every_demo_script_listed_in_readme():
    readme = (DEMO / "README.md").read_text(encoding="utf-8")
    assert sorted(p.stem for p in DEMO.glob("demo-*.py")) == sorted(SCRIPTS)
    assert all(f"`{name}`" in readme or name in readme for name in SCRIPTS)


def test_reference_run_and_broken_file(demo, home):
    for name in ("demo-wb-prices", "demo-ozon-prices"):
        code, data = demo("run", name, "--follow")
        assert code == 0 and data["run"]["status"] == "ok", data
    refs = ("--set", "wb=last:demo-wb-prices:prices.csv", "--set", "ozon=last:demo-ozon-prices:prices.csv")
    code, data = demo("run", "demo-update-prices", *refs, "--follow")
    assert code == 0 and data["run"]["status"] == "ok", data
    rows = read_csv(Path(data["run_dir"]) / "output" / "updated.csv")
    # Меньшая из двух цен минус 1: WB 100 + 10i, Ozon 95 + 11i.
    assert [float(r["new_price"]) for r in rows] == [min(100 + 10 * i, 95 + 11 * i) - 1 for i in range(1, 6)]
    code, data = demo("run", "demo-report", "--set", "updated=last:demo-update-prices:@main", "--follow")
    assert code == 0 and data["run"]["status"] == "ok", data

    code, data = demo("run", "demo-ozon-prices", "--set", "broken=true", "--follow")
    assert code == 0
    code, data = demo("run", "demo-update-prices", *refs, "--follow")
    assert code == 1
    assert data["error"]["message"] == "Демо: цены Ozon, prices.csv: в шапке нет колонок: «price», нужна параметру `ozon`"


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
