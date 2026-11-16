"""Файл результата одного запуска в параметре другого (dawg_replay.transfer): ссылки run:/last:, проверка таблиц, повтор."""

import json
import sys

import pytest

from dawg_replay import launch, transfer
from dawg_replay.errors import ReplayError
from transfer_scripts import SOURCE, TARGET


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


def register(app, tmp_path, text, filename):
    f = tmp_path / filename
    f.write_text(text, encoding="utf-8")
    app.registry.add(f)


@pytest.fixture
def scripts(app, tmp_path):
    register(app, tmp_path, SOURCE.format(name="wb", title="Цены WB"), "wb.py")
    register(app, tmp_path, SOURCE.format(name="ozon", title="Цены Ozon"), "ozon.py")
    register(app, tmp_path, TARGET.format(name="upd", version=1, extra=""), "upd.py")
    return app


def run_source(app, name="wb", **sets):
    args = [f"{k}={v}" for k, v in sets.items()]
    prepared = launch.prepare(app, name, sets=args)
    return launch.supervise(app, prepared.run.id, prepared.payload())


def close(app, prepared):
    app.runs.finish(prepared.run.id, status="ok", reason=None)


def test_refs_and_suitable():
    assert transfer.parse_ref("run:20261116-120000-abcdef:prices.csv") == transfer.Ref("prices.csv", run_id="20261116-120000-abcdef")
    assert transfer.parse_ref("last:wb:prices-*.csv:partial") == transfer.Ref("prices-*.csv", script="wb", partial=True)
    assert transfer.parse_ref("last:wb:@main").text() == "last:wb:@main"
    for bad in ("last:wb", "run::x", "file:a:b", "", None, ["run:a:b"]):
        assert transfer.parse_ref(bad) is None
    assert transfer.suitable("ok", "ok", None) and not transfer.suitable("ok", "partial", None)
    assert transfer.suitable("partial", "partial", None) and transfer.suitable("any", "failed", "timeout")
    assert transfer.suitable("any", "failed", "lost")
    for reason in ("not_started", "skipped", "cancelled"):
        assert not transfer.suitable("any", "failed", reason)


def test_columns_mismatch_stops_before_start(scripts):
    app = scripts
    run_source(app, csv="code,price\nA1,10")
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd", sets=["items=last:wb:prices.csv"])
    assert e.value.code == "input_error"
    assert e.value.message.startswith("Цены WB, prices.csv: ")
    assert "sku" in e.value.message and e.value.message.endswith("нужна параметру `items`")
    assert app.runs.list("upd") == []  # запуск не создан


def test_missing_file_and_main(scripts):
    app = scripts
    src = run_source(app)
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd", sets=["items=last:wb:nothing.csv"])
    assert e.value.message == f"Цены WB: в запуске {src.id} нет файла nothing.csv, он нужен параметру `items`"
    prepared = launch.prepare(app, "upd", sets=["items=last:wb:@main"])
    assert len(prepared.values["items"]) == 2
    close(app, prepared)


def test_fill_run_takes_given_run(scripts):
    """Шаг проекта берёт файл из конкретного запуска источника, а не из последнего."""
    app = scripts
    first = run_source(app, csv="sku\nFIRST")
    run_source(app, csv="sku\nSECOND")
    p = app.registry.manifest("upd").param("items")
    fill = transfer.fill_run(app, p, app.runs.get(first.id), "prices.csv", node="wb1")
    assert [r["sku"] for r in fill.value] == ["FIRST"]
    assert fill.entry == {"param": "items", "source": "wb", "run": first.id, "file": "prices.csv",
                          "files": ["prices.csv"], "node": "wb1"}
    with pytest.raises(ReplayError) as e:
        transfer.fill_run(app, app.registry.manifest("upd").param("keys"), app.runs.get(first.id), "prices.csv")
    assert e.value.code == "input_error"


def test_refs_in_set(scripts):
    app = scripts
    first = run_source(app, csv="sku\nFIRST")
    run_source(app, outcome="partial", csv="sku\nPART")
    prepared = launch.prepare(app, "upd", sets=[f"items=run:{first.id}:prices.csv", "doc=last:wb:report-*.txt"])
    assert [r["sku"] for r in prepared.values["items"]] == ["FIRST"]
    rec = app.runs.get(prepared.run.id)
    assert [e["ref"] for e in rec.inputs] == [f"run:{first.id}:prices.csv", "last:wb:report-*.txt"]
    assert rec.inputs[1]["run"] == first.id  # last без :partial берёт успешный
    close(app, prepared)
    prepared = launch.prepare(app, "upd", sets=["items=last:wb:prices.csv:partial"])
    assert [r["sku"] for r in prepared.values["items"]] == ["PART"]
    close(app, prepared)
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd", sets=["items=run:nope:prices.csv", "doc=last:ozon:x.txt"])
    assert e.value.code == "input_error" and e.value.details == [
        "`items`: запуска nope нет", "Цены Ozon: нет результата (последний успешный), он нужен параметру `doc`"]
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd", sets=["keys=last:wb:prices.csv"])
    assert e.value.code == "bad_params" and "секретный" in e.value.message


def test_ref_like_text_stays_text(scripts):
    app = scripts
    prepared = launch.prepare(app, "upd", sets=['items=[{"sku": "A", "_row": 2}]', "note=last:wb:prices.csv"])
    assert prepared.values["note"] == "last:wb:prices.csv" and app.runs.get(prepared.run.id).inputs == []
    close(app, prepared)


def test_ref_in_preset(scripts):
    app = scripts
    run_source(app, csv="sku\nREF")
    m = app.registry.manifest("upd")
    p = app.presets.save("upd", "по ссылке", m, {"items": "last:wb:prices.csv"})
    assert p.results == {"items": "last:wb:prices.csv"} and p.missing == {} and p.params == {}
    assert p.to_dict()["results"] == {"items": "last:wb:prices.csv"}
    prepared = launch.prepare(app, "upd", preset="по ссылке", strict_preset=True)
    assert [r["sku"] for r in prepared.values["items"]] == ["REF"]
    close(app, prepared)


def test_again_keeps_same_files(scripts):
    app = scripts
    run_source(app, csv="sku\nOLD")
    prepared = launch.prepare(app, "upd", sets=["items=last:wb:prices.csv", "doc=last:wb:report-*.txt"])
    first = launch.supervise(app, prepared.run.id, prepared.payload())
    run_source(app, csv="sku\nNEW")  # новый результат источника повтор не берёт
    values = launch.again_values(app, first)
    again = launch.prepare(app, "upd", values=values, inputs_of=first.id)
    assert [r["sku"] for r in again.values["items"]] == ["OLD"]
    rec = app.runs.get(again.run.id)
    assert [(e["param"], e["run"], e["from_run"]) for e in rec.inputs] == [
        ("items", first.inputs[0]["run"], first.id), ("doc", first.inputs[1]["run"], first.id)]
    close(app, again)


def test_cli_run_with_ref(scripts, cli):
    src = run_source(scripts, csv="sku\nCLI")
    code, data = cli("run", "upd", "--param", f"items=run:{src.id}:prices.csv", "--follow")
    assert code == 0 and data["run"]["status"] == "ok"
    rec = scripts.runs.get(data["run"]["id"])
    assert rec.inputs[0]["ref"] == f"run:{src.id}:prices.csv"
    assert json.loads((scripts.paths.run_dir(rec.id) / "output" / "got.json").read_text("utf-8")) == ["CLI"]
