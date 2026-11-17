"""Объявленные выходы скрипта (контракт 1.4): сверка после запуска, тихая поломка, выходы для связей."""

import json
import sys

import pytest

from dawg_replay import launch, outputs, repair
from dawg_replay.errors import ReplayError

SCRIPT = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "{contract}"
# name = "{name}"
# title = "Цены"
# version = {version}
#
# [[params]]
# name = "mode"
# type = "string"
# title = "Режим"
# default = "ok"
{outputs}# ///
import sys
import dawg_replay_kit as kit

mode = kit.params.mode
out = kit.output_dir
if mode != "nothing":
    (out / "prices.csv").write_text("sku,price\\n1,10\\n", encoding="utf-8")
if mode == "all":
    (out / "shots").mkdir()
    (out / "shots" / "page-1.png").write_bytes(b"png")
if mode == "fail":
    sys.exit(2)
kit.item("r1", "ok")
kit.result("ok", "готово", main=None if mode == "nothing" else "prices.csv")
'''

DECLARED = '''\
#
# [[outputs]]
# name = "prices.csv"
# title = "Цены"
# columns = ["sku", "price"]
# main = true
#
# [[outputs]]
# name = "shots/page-*.png"
# title = "Скриншоты"
'''


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def add(app, tmp_path):
    def add(name="prices", declared=True, version=1, contract="1.4"):
        f = tmp_path / "src" / f"{name}-{version}.py"
        f.parent.mkdir(exist_ok=True)
        f.write_text(SCRIPT.format(name=name, version=version, contract=contract,
                                   outputs=DECLARED if declared else ""), encoding="utf-8")
        app.registry.add(f)
        return name

    return add


def run(app, name, mode="ok"):
    prepared = launch.prepare(app, name, values={"mode": mode})
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    return app.runs.get(rec.id)


def journal(app, run_id):
    lines = (app.paths.run_dir(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e.get("source") == repair.MISSING_SOURCE]


def test_all_declared_present(app, add):
    rec = run(app, add(), "all")
    assert rec.status == "ok"
    assert journal(app, rec.id) == []
    assert repair.suspicion(app, rec) is None


def test_missing_output_journal_and_suspicion(app, add):
    rec = run(app, add(), "ok")  # скриншотов нет
    assert rec.status == "ok"
    lines = journal(app, rec.id)
    assert [(e["level"], e["output"]) for e in lines] == [("warn", "shots/page-*.png")]
    assert lines[0]["msg"] == "Нет файла по шаблону «Скриншоты» (shots/page-*.png), который скрипт объявил в outputs."
    sus = repair.suspicion(app, rec)
    assert sus["kind"] == "missing_output" and sus["missing"] == ["shots/page-*.png"]
    assert "нет файла, которые скрипт обещал отдать: shots/page-*.png" in sus["text"]
    # повторная сверка строк не дублирует
    assert outputs.check_run(app, rec.id) == ["shots/page-*.png"]
    assert len(journal(app, rec.id)) == 1
    # и подсказка для агента это упоминает
    assert "shots/page-*.png" in repair.fix_prompt(app, rec)["text"]


def test_optional_output_absent_is_fine(app, add, tmp_path):
    """`optional = true` (ветвление): файла нет, и это не предупреждение и не тихая поломка."""
    f = tmp_path / "src" / "branchy.py"
    f.parent.mkdir(exist_ok=True)
    f.write_text(SCRIPT.format(name="branchy", version=1, contract="1.4",
                               outputs=DECLARED + "# optional = true\n"), encoding="utf-8")
    app.registry.add(f)
    rec = run(app, "branchy", "ok")  # prices.csv есть, скриншотов нет
    assert rec.status == "ok" and journal(app, rec.id) == []
    assert outputs.check_run(app, rec.id) == []
    assert repair.suspicion(app, rec) is None
    got = outputs.script_outputs(app, "branchy")["outputs"]
    assert [(o["name"], o["optional"]) for o in got] == [("prices.csv", False), ("shots/page-*.png", True)]


def test_missing_both(app, add):
    rec = run(app, add(), "nothing")
    assert [e["output"] for e in journal(app, rec.id)] == ["prices.csv", "shots/page-*.png"]
    assert "нет файлов" in repair.suspicion(app, rec)["text"]


def test_failed_run_not_checked(app, add):
    rec = run(app, add(), "fail")
    assert rec.status == "failed"
    assert journal(app, rec.id) == []
    assert outputs.check_run(app, rec.id) == []


def test_script_without_outputs_as_before(app, add):
    name = add(declared=False, contract="1.3")
    rec = run(app, name, "nothing")
    assert rec.status == "ok" and journal(app, rec.id) == []
    assert outputs.check_run(app, rec.id) == []


def test_missing_output_in_telegram_text(app, add, monkeypatch):
    from dawg_replay import tg_notify, tg_rules
    from dawg_replay.app import App

    rec = run(app, add(), "ok")
    monkeypatch.setattr(tg_rules, "connected", lambda a: True)
    with App(app.paths.home) as a:
        a.set_config("telegram_suspicious", "on")
        plan = tg_rules.plan(a, a.runs.get(rec.id), repair.suspicion(a, a.runs.get(rec.id)))
        text = tg_notify.compose(a, a.runs.get(rec.id), plan).text
    assert "shots/page-*.png" in text


def test_script_outputs_declared(app, add):
    name = add()
    got = outputs.script_outputs(app, name)
    assert got["source"] == "declared" and got["run_id"] is None
    assert got["outputs"] == [
        {"name": "prices.csv", "title": "Цены", "columns": ["sku", "price"], "main": True, "pattern": False,
         "optional": False},
        {"name": "shots/page-*.png", "title": "Скриншоты", "columns": None, "main": False, "pattern": True,
         "optional": False},
    ]


def test_script_outputs_from_last_run(app, add):
    name = add(declared=False)
    assert outputs.script_outputs(app, name) == {"source": "none", "run_id": None, "outputs": [], "values": []}
    run(app, name, "fail")  # упавший не годится
    assert outputs.script_outputs(app, name)["source"] == "none"
    good = run(app, name, "all")
    run(app, name, "fail")
    got = outputs.script_outputs(app, name)
    assert got["source"] == "last_run" and got["run_id"] == good.id
    assert [(o["name"], o["main"]) for o in got["outputs"]] == [("prices.csv", True), ("shots/page-1.png", False)]


def test_script_outputs_unknown_script(app):
    with pytest.raises(ReplayError) as e:
        outputs.script_outputs(app, "nope")
    assert e.value.code == "script_not_found"


def test_output_files_skip_symlinks(tmp_path):
    out = tmp_path / "output"
    (out / "sub").mkdir(parents=True)
    (out / "b.csv").write_text("x")
    (out / "sub" / "a.json").write_text("x")
    (tmp_path / "secret.txt").write_text("x")
    try:
        (out / "link.txt").symlink_to(tmp_path / "secret.txt")
    except OSError:
        pytest.skip("символические ссылки недоступны")
    assert outputs.output_files(out) == ["b.csv", "sub/a.json"]
    assert outputs.output_files(tmp_path / "none") == []
