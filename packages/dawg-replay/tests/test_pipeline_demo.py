"""Демо связей examples/pipeline-demo (dr-108): команды из README демо и плана приёмки M6 работают как написано.

Без браузера, сети, системного планировщика и системного хранилища паролей: скрипты запускаются интерпретатором
тестов, дом приложения временный.
"""

import csv
import json
import sys
import threading
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


# --- автозапуск по связям (dr-106): сценарии Ц3, К2 и Р плана приёмки ---------------------------


@pytest.fixture
def autorun(demo, home, kr, monkeypatch):
    """Получатель запускается в этом процессе (как процесс-супервизор, только синхронно)."""
    from dawg_replay import chain
    from dawg_replay.app import App

    started = []

    def spawn(app, prepared):
        started.append(prepared.run.script_name)

        def work():  # свой поток, как свой процесс-супервизор: решения по связям принимает он
            with App(home, keyring_backend=kr) as a:
                launch.supervise(a, prepared.run.id, prepared.payload())

        t = threading.Thread(target=work)
        t.start()
        t.join()

    monkeypatch.setattr(chain, "_spawn", spawn)
    monkeypatch.setattr(chain, "_spawn_waiting", lambda app, *a: None)
    return started


def runs_of(cli, name):
    code, data = cli("runs", name)
    assert code == 0
    return data["runs"]


def test_chain_runs_itself(demo, autorun):
    chain(demo)
    demo("run", "demo-wb-prices", "--follow")
    assert autorun == []  # обновление ждёт Ozon
    code, data = demo("link", "show", "demo-wb-prices", "demo-update-prices")
    assert "ждёт остальных источников (Демо: цены Ozon)" in json.dumps(data, ensure_ascii=False)
    demo("run", "demo-ozon-prices", "--follow")
    assert autorun == ["demo-update-prices", "demo-report"]  # обновление один раз, за ним отчёт
    (upd,) = runs_of(demo, "demo-update-prices")
    (rep,) = runs_of(demo, "demo-report")
    assert upd["status"] == "ok" and rep["status"] == "ok" and rep["summary"].startswith("товаров 5")
    code, data = demo("logs", upd["id"], "--events")
    assert any("Запущен после: Демо: цены Ozon" in str(e.get("msg")) for e in data["events"])


def test_broken_source_file_stops_target_before_start(demo, autorun):
    chain(demo)
    demo("run", "demo-wb-prices", "--follow")
    demo("run", "demo-ozon-prices", "--set", "broken=true", "--follow")
    (upd,) = runs_of(demo, "demo-update-prices")
    assert upd["status"] == "failed" and upd["reason"] == "not_started"
    assert "prices.csv: в шапке нет колонок: «price», нужна параметру `ozon`" in upd["error"]
    assert runs_of(demo, "demo-report") == []


def test_cycle_stops_at_limit(demo, autorun):
    demo("link", "add", "demo-ping", "demo-pong", "--file", "@main=ball")
    demo("link", "add", "demo-pong", "demo-ping", "--file", "@main=ball", "--limit-per-day", "3")
    demo("run", "demo-ping", "--follow")
    pings, pongs = runs_of(demo, "demo-ping"), runs_of(demo, "demo-pong")
    assert (len(pings), len(pongs)) == (4, 4)  # ручной пинг и 3 по связи понг → пинг, на каждый пинг свой понг
    assert max(int(r["summary"].split()[1].rstrip(":")) for r in pings + pongs) == 8
    code, data = demo("link", "show", "demo-pong", "demo-ping")
    assert "не запущен: исчерпан предел 3 в сутки" in json.dumps(data, ensure_ascii=False)
