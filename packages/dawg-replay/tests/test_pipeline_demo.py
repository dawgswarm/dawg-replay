"""Демо связей examples/pipeline-demo (dr-108): команды из README демо и плана приёмки M6 работают как написано.

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
    monkeypatch.setenv("DAWG_REPLAY_CHAIN_WAITER", "off")
    for name in SCRIPTS:
        code, data = cli("add", str(DEMO / f"{name}.py"))
        assert code == 0, data
    return cli


def chain(cli):
    for argv in (
        ("link", "add", "demo-wb-prices", "demo-update-prices", "--file", "prices.csv=wb"),
        ("link", "add", "demo-ozon-prices", "demo-update-prices", "--file", "prices.csv=ozon"),
        ("link", "add", "demo-update-prices", "demo-report", "--file", "@main=updated"),
        ("link", "mode", "demo-update-prices", "all", "--wait", "5m", "--on-timeout", "skip"),
    ):
        code, data = cli(*argv)
        assert code == 0, data


def test_every_demo_script_listed_in_readme():
    readme = (DEMO / "README.md").read_text(encoding="utf-8")
    assert sorted(p.stem for p in DEMO.glob("demo-*.py")) == sorted(SCRIPTS)
    assert all(f"`{name}`" in readme or name in readme for name in SCRIPTS)


def test_chain_builds_and_checks_clean(demo):
    chain(demo)
    code, data = demo("link", "check")
    assert code == 0 and data["problems"] == [] and data["links"] == 3


def test_column_mismatch_is_a_warning_at_save(demo):
    chain(demo)
    # Один параметр получает файл одной связи: сначала конфликт.
    code, data = demo("link", "add", "demo-wb-prices", "demo-report", "--file", "prices.csv=updated", "--after", "never")
    assert code == 1 and "уже передаёт файл связь demo-update-prices → demo-report" in data["error"]["message"]
    demo("link", "remove", "demo-update-prices", "demo-report")
    code, data = demo("link", "add", "demo-wb-prices", "demo-report", "--file", "prices.csv=updated", "--after", "never")
    assert code == 0
    code, data = demo("link", "check")
    assert code == 0  # предупреждение, не ошибка
    assert [p["level"] for p in data["problems"]] == ["warning"]
    assert "в `prices.csv` нет колонки `new_price`, нужны параметру `updated`" in data["problems"][0]["text"]


def test_cycle_needs_a_limit(demo):
    code, _ = demo("link", "add", "demo-ping", "demo-pong", "--file", "@main=ball")
    assert code == 0
    code, data = demo("link", "add", "demo-pong", "demo-ping", "--file", "@main=ball")
    assert code == 1 and "круг без предела: demo-pong → demo-ping → demo-pong" in data["error"]["message"]
    code, data = demo("link", "add", "demo-pong", "demo-ping", "--file", "@main=ball", "--limit-per-day", "3")
    assert code == 0
    code, data = demo("link", "check")
    assert data["problems"] == []


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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

    code, data = demo("run", "demo-ozon-prices", "--set", "broken=true", "--follow")
    assert code == 0
    code, data = demo("run", "demo-update-prices", *refs, "--follow")
    assert code == 1
    assert data["error"]["message"] == "Демо: цены Ozon, prices.csv: в шапке нет колонок: «price», нужна параметру `ozon`"
