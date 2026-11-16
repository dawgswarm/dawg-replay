"""Связи между скриптами (dawg_replay.links): хранение, проверка, передача файлов в запуск, CLI link."""

import json
import sys

import pytest

from dawg_replay import launch, links
from dawg_replay.errors import ReplayError
from dawg_replay.links import Transfer
from link_scripts import SOURCE, TARGET

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


# --- хранение ---


def test_add_list_update_remove(scripts):
    app = scripts
    link, warnings = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    assert warnings == []
    assert link.to_dict() | {"created_at": "", "updated_at": ""} == {
        "id": link.id, "source": "wb", "target": "upd", "cond": "ok", "starts": True, "after": "ok", "preset": None,
        "enabled": True, "limit_per_day": None, "files": [{"file": "prices.csv", "param": "items"}],
        "created_at": "", "updated_at": "",
    }
    assert app.links.find("wb", "upd") == link
    assert [x.id for x in app.links.list("upd")] == [link.id] == [x.id for x in app.links.incoming("upd")]
    assert app.links.outgoing("upd") == []
    with pytest.raises(ReplayError) as e:
        app.links.add("wb", "upd", files=[Transfer("prices.csv", "doc")])
    assert e.value.code == "link_exists"

    link, _ = app.links.update(link.id, cond="partial", files=[Transfer("@main", "items"), Transfer("report-*.txt", "doc")],
                               limit_per_day=3)
    assert (link.cond, link.limit_per_day, [t.label for t in link.files]) == (
        "partial", 3, ["главный файл → items", "report-*.txt → doc"])
    link, _ = app.links.update(link.id, limit_per_day=None, starts=False)
    assert (link.limit_per_day, link.after) == (None, "never")
    assert app.links.set_enabled(link.id, False).enabled is False
    app.links.log(link.id, "not_started", "не запущен: Цены WB упал", source_run="r1")
    assert app.links.journal(link.id)[0]["text"] == "не запущен: Цены WB упал"
    app.links.remove(link.id)
    assert app.links.list() == []
    with pytest.raises(ReplayError) as e:
        app.links.get(link.id)
    assert e.value.code == "link_not_found"


def test_validation_errors(scripts):
    app = scripts

    def problems(**kw):
        source = kw.pop("source", "wb")
        with pytest.raises(ReplayError) as e:
            app.links.add(source, "upd", **kw)
        assert e.value.code == "link_invalid"
        return e.value.message

    assert "нет параметра `nope`" in problems(files=[Transfer("a.csv", "nope")])
    assert "типа string" in problems(files=[Transfer("a.csv", "note")])
    assert "секретный" in problems(files=[Transfer("a.csv", "keys")])
    assert "ничего не делает" in problems(starts=False)
    assert "нет пресета «ночь»" in problems(preset="ночь")
    assert "скрипта `ghost` нет" in problems(source="ghost")
    assert "выходить из каталога" in problems(files=[Transfer("../x.csv", "items")])
    assert "полный путь" in problems(files=[Transfer("/etc/x.csv", "items")])
    assert "уже передаётся файл этой связи" in problems(files=[Transfer("a.csv", "items"), Transfer("b.csv", "items")])
    app.links.add("ozon", "upd", files=[Transfer("prices.csv", "items")])
    assert "уже передаёт файл связь ozon → upd" in problems(files=[Transfer("prices.csv", "items")])
    with pytest.raises(ReplayError):
        app.links.add("wb", "upd", cond="sometimes")
    with pytest.raises(ReplayError):
        app.links.add("wb", "upd", limit_per_day=0)


def test_cycle_needs_limit(scripts):
    app = scripts
    app.links.add("wb", "ozon")
    app.links.add("ozon", "upd")
    with pytest.raises(ReplayError) as e:
        app.links.add("upd", "wb")
    assert "круг без предела: upd → wb → ozon → upd" in e.value.message
    with pytest.raises(ReplayError):
        app.links.add("wb", "wb")
    # Предел на одной связи круга разрешает его; «только файл» круга не делает.
    back, _ = app.links.add("upd", "wb", limit_per_day=2)
    assert app.links.check() == []
    with pytest.raises(ReplayError) as e:
        app.links.update(back.id, limit_per_day=None)
    assert "круг без предела" in e.value.message


def test_file_only_link_does_not_close_cycle(scripts):
    app = scripts
    app.links.add("wb", "upd")
    # Параметр csv у wb типа text: передать файл нельзя, но круга связь «только файл» не замыкает.
    with pytest.raises(ReplayError) as e:
        app.links.add("upd", "wb", starts=False, files=[Transfer("got.json", "csv")])
    assert "круг" not in e.value.message and "типа text" in e.value.message


def test_outputs_columns_warning(scripts, monkeypatch):
    app = scripts

    class Out:
        def __init__(self, name, columns=None, main=False):
            self.name, self.title, self.columns, self.main = name, name, columns, main

    class WithOutputs:
        def __init__(self, m, outputs):
            self._m, self.outputs = m, outputs

        def __getattr__(self, k):
            return getattr(self._m, k)

    real = app.registry.manifest

    def manifest(name, version=None):
        m = real(name, version)
        if name == "wb":
            return WithOutputs(m, (Out("prices.csv", ["price"], main=True), Out("report-*.txt")))
        return m

    monkeypatch.setattr(app.registry, "manifest", manifest)
    link, warnings = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items"), Transfer("stock.csv", "doc"),
                                                       Transfer("report-2.txt", "docs")])
    texts = [w.text for w in warnings]
    assert texts == ["wb → upd: в `prices.csv` нет колонки `sku`, нужны параметру `items`",
                     "wb → upd: файл `stock.csv` не объявлен в выходах скрипта `wb`"]
    assert all(w.level == "warning" for w in warnings)
    link, warnings = app.links.update(link.id, files=[Transfer("@main", "items")])
    assert [w.text for w in warnings] == ["wb → upd: в `prices.csv` нет колонки `sku`, нужны параметру `items`"]
    assert [p.text for p in app.links.check("upd")] == [w.text for w in warnings]


def test_target_mode(scripts):
    app = scripts
    assert app.links.mode("upd").to_dict() == {"script": "upd", "mode": "all", "wait_seconds": 21600, "on_timeout": "skip"}
    m = app.links.set_mode("upd", "any", 3600, "run")
    assert app.links.mode("upd") == m
    assert app.links.set_mode("upd", wait_seconds=7200).mode == "any"
    for kw in ({"mode": "some"}, {"on_timeout": "wait"}, {"wait_seconds": 5}):
        with pytest.raises(ReplayError):
            app.links.set_mode("upd", **kw)
    with pytest.raises(ReplayError):
        app.links.set_mode("ghost", "all")


def test_remove_script_drops_links(scripts):
    app = scripts
    a, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    app.links.add("ozon", "wb")
    app.links.set_mode("wb", "any")
    r = app.remove("wb")
    assert len(r["links_removed"]) == 2
    assert app.links.list() == [] and app.links.mode("wb").mode == "all"


def test_refs_and_suitable():
    assert links.parse_ref("run:20261116-120000-abcdef:prices.csv") == links.Ref("prices.csv", run_id="20261116-120000-abcdef")
    assert links.parse_ref("last:wb:prices-*.csv:partial") == links.Ref("prices-*.csv", script="wb", partial=True)
    assert links.parse_ref("last:wb:@main").text() == "last:wb:@main"
    for bad in ("last:wb", "run::x", "file:a:b", "", None, ["run:a:b"]):
        assert links.parse_ref(bad) is None
    assert links.suitable("ok", "ok", None) and not links.suitable("ok", "partial", None)
    assert links.suitable("partial", "partial", None) and links.suitable("any", "failed", "timeout")
    assert links.suitable("any", "failed", "lost")
    for reason in ("not_started", "skipped", "cancelled"):
        assert not links.suitable("any", "failed", reason)


# --- подстановка в запуск ---


def test_link_fills_table_and_records_inputs(scripts):
    app = scripts
    src = run_source(app)
    assert src.status == "ok"
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items"), Transfer("report-*.txt", "doc"),
                                                Transfer("report-*.txt", "docs")])
    prepared = launch.prepare(app, "upd")
    v = prepared.values
    assert [r["sku"] for r in v["items"]] == ["A1", "B2"]
    assert v["doc"]["name"] == "report-2.txt"  # шаблон: самый новый
    assert sorted(f["name"] for f in v["docs"]) == ["report-1.txt", "report-2.txt"]  # multiple: все
    run_dir = app.paths.run_dir(prepared.run.id)
    assert (run_dir / "input" / "items" / "prices.csv").is_file()
    assert v["doc"]["path"].startswith(str((run_dir / "input").resolve()))
    rec = app.runs.get(prepared.run.id)
    assert rec.inputs == [
        {"param": "items", "source": "wb", "run": src.id, "file": "prices.csv", "files": ["prices.csv"], "link_id": link.id},
        {"param": "doc", "source": "wb", "run": src.id, "file": "report-*.txt", "files": ["report-2.txt"], "link_id": link.id},
        {"param": "docs", "source": "wb", "run": src.id, "file": "report-*.txt", "files": ["report-2.txt", "report-1.txt"],
         "link_id": link.id},
    ]
    assert rec.trigger == "manual" and rec.link_id is None
    done = launch.supervise(app, prepared.run.id, prepared.payload())
    assert done.status == "ok"
    assert json.loads((run_dir / "output" / "got.json").read_text("utf-8")) == ["A1", "B2"]


def test_explicit_value_wins_and_disabled_link_ignored(scripts):
    app = scripts
    run_source(app)
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    prepared = launch.prepare(app, "upd", sets=['items=[{"sku": "Z9", "_row": 2}]'])
    assert [r["sku"] for r in prepared.values["items"]] == ["Z9"]
    assert app.runs.get(prepared.run.id).inputs == []
    close(app, prepared)
    app.links.set_enabled(link.id, False)
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd")
    assert e.value.code == "bad_params"  # выключенная связь ничего не даёт: таблицы нет


def test_link_beats_preset_value(scripts):
    app = scripts
    run_source(app)
    m = app.registry.manifest("upd")
    app.presets.save("upd", "ночь", m, {"items": [{"sku": "P1"}], "note": "из пресета"})
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")], preset="ночь")
    prepared = launch.prepare(app, "upd", preset="ночь")
    assert [r["sku"] for r in prepared.values["items"]] == ["A1", "B2"] and prepared.values["note"] == "из пресета"
    close(app, prepared)


def test_preset_without_fed_param_is_not_outdated(scripts):
    app = scripts
    run_source(app)
    m = app.registry.manifest("upd")
    app.presets.save("upd", "ночь", m, {"note": "x"})
    assert app.presets.get("upd", "ночь", m).missing == {"items": "Товары"}
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd", preset="ночь", strict_preset=True)
    assert e.value.code == "preset_outdated"
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    prepared = launch.prepare(app, "upd", preset="ночь", strict_preset=True)
    assert len(prepared.values["items"]) == 2
    close(app, prepared)


def test_no_suitable_result(scripts):
    app = scripts
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items"), Transfer("report-1.txt", "doc")])
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd")
    # Обязательная таблица: причина понятная; необязательный файл просто не заполнен.
    assert e.value.code == "link_input"
    assert e.value.message == "Цены WB: нет результата (последний успешный), он нужен параметру `items`"
    run_source(app, outcome="partial")
    with pytest.raises(ReplayError):
        launch.prepare(app, "upd")
    app.links.update(link.id, cond="partial")
    prepared = launch.prepare(app, "upd")
    assert prepared.run.id and prepared.values["doc"]["name"] == "report-1.txt"
    close(app, prepared)


def test_failed_source_falls_back_to_last_ok(scripts):
    app = scripts
    good = run_source(app, csv="sku\nOLD")
    run_source(app, outcome="failed", csv="sku\nNEW")
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    prepared = launch.prepare(app, "upd")
    assert [r["sku"] for r in prepared.values["items"]] == ["OLD"]
    assert app.runs.get(prepared.run.id).inputs[0]["run"] == good.id
    close(app, prepared)


def test_columns_mismatch_is_link_input(scripts):
    app = scripts
    run_source(app, csv="code,price\nA1,10")
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd")
    assert e.value.code == "link_input"
    assert e.value.message.startswith("Цены WB, prices.csv: ")
    assert "sku" in e.value.message and e.value.message.endswith("нужна параметру `items`")
    assert app.runs.list("upd") == []  # запуск не создан


def test_missing_file_and_main(scripts):
    app = scripts
    src = run_source(app)
    link, _ = app.links.add("wb", "upd", files=[Transfer("nothing.csv", "items")])
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd")
    assert e.value.message == f"Цены WB: в запуске {src.id} нет файла nothing.csv, он нужен параметру `items`"
    app.links.update(link.id, files=[Transfer("@main", "items")])
    prepared = launch.prepare(app, "upd")
    assert len(prepared.values["items"]) == 2
    close(app, prepared)


def test_link_sources_pick_given_run(scripts):
    app = scripts
    first = run_source(app, csv="sku\nFIRST")
    run_source(app, csv="sku\nSECOND")
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    prepared = launch.prepare(app, "upd", link_id=link.id, source_run=first.id)
    rec = app.runs.get(prepared.run.id)
    assert [r["sku"] for r in prepared.values["items"]] == ["FIRST"]
    assert (rec.trigger, rec.link_id, rec.source_run) == ("link", link.id, first.id)
    close(app, prepared)


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
    assert e.value.code == "link_input" and e.value.details == [
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
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items"), Transfer("report-*.txt", "doc")])
    prepared = launch.prepare(app, "upd")
    first = launch.supervise(app, prepared.run.id, prepared.payload())
    run_source(app, csv="sku\nNEW")  # новый результат источника повтор не берёт
    values = launch.again_values(app, first)
    again = launch.prepare(app, "upd", values=values, inputs_of=first.id)
    assert [r["sku"] for r in again.values["items"]] == ["OLD"]
    rec = app.runs.get(again.run.id)
    assert [(e["param"], e["run"], e["from_run"]) for e in rec.inputs] == [
        ("items", first.inputs[0]["run"], first.id), ("doc", first.inputs[1]["run"], first.id)]
    close(app, again)


def test_draft_ignores_links(scripts, tmp_path):
    app = scripts
    run_source(app)
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    f = tmp_path / "draft" / "script.py"
    f.parent.mkdir()
    f.write_text(TARGET.format(name="upd", version=1, extra=""), encoding="utf-8")
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, str(f))
    assert e.value.code == "bad_params"


def test_record_not_started_for_link(scripts):
    app = scripts
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    try:
        launch.prepare(app, "upd", link_id=link.id, source_run="r0")
    except ReplayError as e:
        rec = launch.record_not_started(app, None, "upd", None, e, link_id=link.id, source_run="r0")
    assert (rec.status, rec.reason, rec.trigger, rec.link_id, rec.source_run) == ("failed", "not_started", "link", link.id, "r0")
    assert "Цены WB: нет результата" in rec.error
    skipped = launch.record_not_started(app, None, "upd", None, ReplayError("link_timeout", "срок вышел"),
                                        link_id=link.id, reason="skipped")
    assert skipped.label == "failed (skipped)"


def test_check_finds_broken_target(scripts, tmp_path):
    app = scripts
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "doc")])
    assert app.links.check() == []
    # Новая версия получателя без параметра doc: связь остаётся, проверка её показывает.
    text = TARGET.format(name="upd", version=2, extra="").replace(
        '# [[params]]\n# name = "doc"\n# type = "file"\n# title = "Отчёт"\n#\n', "")
    register(app, tmp_path, text, "upd2.py")
    assert [p.text for p in app.links.check("wb")] == ["wb → upd: у скрипта `upd` нет параметра `doc`"]
    run_source(app)
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "upd", sets=['items=[{"sku": "A", "_row": 2}]'])
    assert e.value.code == "link_input" and "link check" in e.value.message


# --- CLI ---


def test_cli_link(scripts, cli):
    code, data = cli("link", "add", "wb", "upd", "--file", "prices.csv=items", "--file", "@main=doc", "--preset", "")
    assert code == 0 and data["link"]["files"] == [{"file": "prices.csv", "param": "items"}, {"file": "@main", "param": "doc"}]
    lid = data["link"]["id"]
    code, data = cli("link", "add", "wb", "upd")
    assert code != 0 and data["error"]["code"] == "link_exists"
    code, data = cli("link", "add", "ozon", "upd", "--after", "never", "--partial", "--file", "report-*.txt=docs")
    assert code == 0 and (data["link"]["cond"], data["link"]["starts"]) == ("partial", False)
    code, data = cli("link", "add", "upd", "wb", "--after", "partial", "--partial")
    assert code != 0 and "--partial только с --after never" in data["error"]["message"]

    code, data = cli("link", "list", "upd")
    assert code == 0 and [(x["source"], x["after"]) for x in data["links"]] == [("ozon", "never"), ("wb", "ok")]
    code, data = cli("link", "set", "wb", "upd", "--after", "any", "--limit-per-day", "2", "--file", "prices.csv=items")
    assert code == 0 and (data["link"]["cond"], data["link"]["limit_per_day"], len(data["link"]["files"])) == ("any", 2, 1)
    code, data = cli("link", "set", str(lid), "--no-limit")
    assert code == 0 and data["link"]["limit_per_day"] is None
    code, data = cli("link", "set", str(lid))
    assert code != 0 and data["error"]["code"] == "bad_link"
    code, data = cli("link", "disable", "wb", "upd")
    assert code == 0 and data["link"]["enabled"] is False
    code, data = cli("link", "enable", str(lid))
    assert code == 0 and data["link"]["enabled"] is True

    code, data = cli("link", "mode", "upd", "any", "--wait", "90m", "--on-timeout", "run")
    assert code == 0 and data["target_mode"] == {"script": "upd", "mode": "any", "wait_seconds": 5400, "on_timeout": "run"}
    code, data = cli("link", "mode", "upd")
    assert code == 0 and data["target_mode"]["mode"] == "any"
    code, data = cli("link", "mode", "upd", "--wait", "soon")
    assert code != 0

    code, data = cli("link", "show", "wb", "upd")
    assert code == 0 and data["link"]["id"] == lid and data["target_mode"]["wait_seconds"] == 5400
    assert data["journal"] == [] and data["problems"] == []
    code, data = cli("link", "check")
    assert code == 0 and data == {"ok": True, "problems": [], "links": 2}

    code, data = cli("link", "remove", "wb", "upd")
    assert code == 0 and data["removed"]["id"] == lid
    code, data = cli("link", "show", "wb", "upd")
    assert code != 0 and data["error"]["code"] == "link_not_found"


def test_cli_check_reports_errors(scripts, cli, tmp_path):
    cli("link", "add", "wb", "upd", "--file", "prices.csv=doc")
    text = TARGET.format(name="upd", version=2, extra="").replace(
        '# [[params]]\n# name = "doc"\n# type = "file"\n# title = "Отчёт"\n#\n', "")
    register(scripts, tmp_path, text, "upd2.py")
    code, data = cli("link", "check", "upd")
    assert code != 0 and data["error"]["code"] == "link_check_failed"
    assert data["error"]["details"][0]["text"] == "wb → upd: у скрипта `upd` нет параметра `doc`"


def test_cli_run_with_ref(scripts, cli):
    src = run_source(scripts, csv="sku\nCLI")
    code, data = cli("run", "upd", "--param", f"items=run:{src.id}:prices.csv", "--follow")
    assert code == 0 and data["run"]["status"] == "ok"
    rec = scripts.runs.get(data["run"]["id"])
    assert rec.inputs[0]["ref"] == f"run:{src.id}:prices.csv"
    assert json.loads((scripts.paths.run_dir(rec.id) / "output" / "got.json").read_text("utf-8")) == ["CLI"]


def test_link_run_counts_as_scheduled_for_telegram(scripts):
    from dawg_replay import tg_rules

    app = scripts
    run_source(app)
    tg_rules.set_general(app, "done_scheduled", True)
    tg_rules.set_general(app, "done_manual", "never")
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    by_link = launch.prepare(app, "upd", link_id=link.id)
    by_link = launch.supervise(app, by_link.run.id, by_link.payload())
    manual = launch.prepare(app, "upd")
    manual = launch.supervise(app, manual.run.id, manual.payload())
    assert (by_link.trigger, manual.trigger) == ("link", "manual")
    assert tg_rules.plan(app, by_link) != tg_rules.NOTHING
    assert tg_rules.plan(app, manual) == tg_rules.NOTHING
