"""Демо examples/pipeline-demo: проект из двух источников, обновления, склейки и ветки; ссылки last: и колонки.

Без браузера, сети, системного планировщика и системного хранилища паролей: скрипты запускаются интерпретатором
тестов, дом приложения временный.
"""

import csv
import json
import sys
import threading
from pathlib import Path

import pytest

from dawg_replay import builtin, launch
from dawg_replay import project_runs as P
from dawg_replay.app import App

ROOT = Path(__file__).resolve().parents[3]
DEMO = ROOT / "examples" / "pipeline-demo"
SCRIPTS = ["demo-wb-prices", "demo-ozon-prices", "demo-update-prices", "demo-report"]


@pytest.fixture
def demo(cli, monkeypatch, tmp_path):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))  # метка «упасть один раз» не в общем временном каталоге
    (tmp_path / "tmp").mkdir()
    monkeypatch.delenv(builtin.ENV_STDLIB, raising=False)  # проекту нужны «Если» и «Склеить таблицы»
    for name in SCRIPTS:
        code, data = cli("add", str(DEMO / f"{name}.py"))
        assert code == 0, data
    return cli


def test_every_demo_script_listed_in_readme():
    readme = (DEMO / "README.md").read_text(encoding="utf-8")
    assert sorted(p.stem for p in DEMO.glob("demo-*.py")) == sorted(SCRIPTS)
    assert all(f"`{name}`" in readme or name in readme for name in SCRIPTS)
    assert "demo-prices.project.toml" in readme


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


@pytest.fixture
def steps(home, kr, monkeypatch):
    """Шаг проекта идёт в своём потоке со своим App, как в своём процессе."""

    def spawn(app, prun_id, node):
        def work():
            with App(home, keyring_backend=kr) as a:
                P.run_step(a, prun_id, node)

        t = threading.Thread(target=work)
        t.start()
        t.join()

    monkeypatch.setattr(P, "_spawn_step", spawn)


def statuses(pr):
    return {s.node: s.status for s in pr.steps}


def test_project_runs_both_sources_branch_and_merge(demo, home, kr, steps):
    code, data = demo("project", "add", str(DEMO / "demo-prices.project.toml"))
    assert code == 0 and data["warnings"] == [], data
    with App(home, keyring_backend=kr) as app:
        pr = P.start(app, "demo-prices")
        assert pr.status == "ok", pr.to_dict()
        assert statuses(pr) == {"wb": "ok", "ozon": "ok", "update": "ok", "all": "ok", "check": "ok",
                                "report": "ok", "few": "off_branch"}
        merged = read_csv(app.paths.run_dir(pr.step("all").run_id) / "output" / "merged.csv")
        assert [r["Площадка"] for r in merged] == ["WB"] * 5 + ["Ozon"] * 5


def test_project_retry_from_failed_source(demo, home, kr, steps, tmp_path):
    text = (DEMO / "demo-prices.project.toml").read_text(encoding="utf-8")
    text = text.replace('id = "ozon"\nscript = "demo-ozon-prices"\n',
                        'id = "ozon"\nscript = "demo-ozon-prices"\nvalues = { outcome = "failed_once" }\n')
    f = tmp_path / "demo-prices.project.toml"
    f.write_text(text, encoding="utf-8")
    assert demo("project", "add", str(f))[0] == 0
    with App(home, keyring_backend=kr) as app:
        pr = P.start(app, "demo-prices")
        assert pr.status == "failed"
        assert statuses(pr) == {"wb": "ok", "ozon": "failed", "update": "skipped", "all": "skipped",
                                "check": "skipped", "report": "skipped", "few": "skipped"}
        assert pr.step("ozon").reason == "упал по просьбе"  # итог скрипта, если ошибки запуска нет
        again = P.retry(app, pr.id)
        assert again.status == "ok" and again.step("wb").reused and not again.step("ozon").reused
        assert statuses(again)["report"] == "ok"
