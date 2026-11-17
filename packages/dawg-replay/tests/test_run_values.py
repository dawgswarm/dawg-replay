"""Выходные значения запусков (контракт 1.5): приём после запуска, секреты, тихая поломка, ссылки =имя."""

import json
import sys

import pytest
from dawg_replay import launch, outputs, repair, run_values, transfer
from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from dawg_replay.runview import RunView

SOURCE = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.5,<2"]
# ///

# /// dawg-script
# contract = "1.5"
# name = "login"
# title = "Вход в кабинет"
# version = 1
#
# [[params]]
# name = "mode"
# type = "string"
# title = "Режим"
# default = "all"
#
# [[outputs]]
# kind = "value"
# name = "token"
# title = "Токен"
# type = "string"
# secret = true
#
# [[outputs]]
# kind = "value"
# name = "orders"
# title = "Заказов"
# type = "int"
#
# [[outputs]]
# kind = "value"
# name = "city"
# type = "string"
# optional = true
# ///
import dawg_replay_kit as kit

mode = kit.params.mode
if mode != "none":
    kit.output("token", "tok-XYZ-123")
if mode == "all":
    kit.output("orders", 42)
    kit.output("city", "Москва")
kit.result("ok", "вошли")
'''

TARGET = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.5,<2"]
# ///

# /// dawg-script
# contract = "1.5"
# name = "orders"
# title = "Заказы"
# version = 1
#
# [[params]]
# name = "limit"
# type = "int"
# title = "Сколько"
# required = true
#
# [[params]]
# name = "label"
# type = "string"
# title = "Подпись"
# default = "-"
# ///
import json
import dawg_replay_kit as kit

(kit.output_dir / "got.json").write_text(json.dumps([kit.params.limit, kit.params.label]), encoding="utf-8")
kit.result("ok", "ок")
'''


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def scripts(app, tmp_path):
    for name, text in (("login", SOURCE), ("orders", TARGET)):
        f = tmp_path / f"{name}.py"
        f.write_text(text, encoding="utf-8")
        app.registry.add(f)
    return app


def run(app, name, **values):
    prepared = launch.prepare(app, name, values=values or None)
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    return app.runs.get(rec.id)


def got(app, rec):
    return json.loads((app.paths.run_dir(rec.id) / "output" / "got.json").read_text(encoding="utf-8"))


def test_values_collected_secret_in_keyring(scripts, kr):
    app = scripts
    rec = run(app, "login")
    assert rec.status == "ok"
    run_dir = app.paths.run_dir(rec.id)
    assert not (run_dir / "values.json").exists()  # забрано и удалено
    assert run_values.read(app, rec.id) == {"token": "tok-XYZ-123", "orders": 42, "city": "Москва"}
    assert run_values.shown(app, rec.id) == [
        {"name": "token", "title": "Токен", "type": "string", "secret": True, "value": None},
        {"name": "orders", "title": "Заказов", "type": "int", "secret": False, "value": 42},
        {"name": "city", "title": None, "type": "string", "secret": False, "value": "Москва"},
    ]
    row = app.db.execute("SELECT value_json FROM run_values WHERE run_id = ? AND name = 'token'", (rec.id,)).fetchone()
    assert row["value_json"] is None  # секрета в БД нет
    assert json.loads(kr.get_password("dawg-replay", f"run:{rec.id}:token")) == "tok-XYZ-123"
    assert "tok-XYZ-123" not in (run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert not [p for p in run_dir.rglob("*") if p.is_file() and b"tok-XYZ-123" in p.read_bytes()]
    assert repair.suspicion(app, rec) is None
    assert run_values.collect(app, rec.id) == []  # повтор ничего не меняет
    assert run_values.read(app, rec.id)["token"] == "tok-XYZ-123"

    run_values.forget(app, rec.id)
    assert run_values.read(app, rec.id) == {}
    assert kr.get_password("dawg-replay", f"run:{rec.id}:token") is None


def test_missing_required_value_is_suspicious(scripts):
    app = scripts
    rec = run(app, "login", mode="token")  # orders не отдан, city необязательный
    assert rec.status == "ok"
    sus = repair.suspicion(app, rec)
    assert sus["kind"] == "missing_output" and sus["missing"] == ["=orders"]
    assert sus["text"] == "Запуск завершился без ошибки, но нет значения, которые скрипт обещал отдать: orders."
    lines = [json.loads(x) for x in (app.paths.run_dir(rec.id) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["msg"] for e in lines if e.get("source") == repair.MISSING_SOURCE] == [
        "Нет значения «Заказов» (orders), которое скрипт объявил в outputs."]
    run_values.collect(app, rec.id)
    assert len([e for e in lines if e.get("source") == repair.MISSING_SOURCE]) == 1
    rec2 = run(app, "login", mode="none")
    assert repair.suspicion(app, rec2)["missing"] == ["=token", "=orders"]


def test_judge_files_and_values():
    sus = repair.judge(None, False, [], ["a.csv", "=token"])
    assert sus["text"] == ("Запуск завершился без ошибки, но нет файла, которые скрипт обещал отдать: a.csv; "
                           "нет значения, которые скрипт обещал отдать: token.")


def test_no_keyring_secret_not_saved(scripts, home, tmp_path):
    class Broken:
        """Хранилища паролей нет: как fail-бэкенд keyring. Ключа DAWG тоже нет, запуску он не нужен."""

        def get_password(self, service, username):
            if username.startswith("run:"):
                raise RuntimeError("нет хранилища")

        def set_password(self, service, username, password):
            raise RuntimeError("нет хранилища")

        def delete_password(self, service, username):
            raise RuntimeError("нет хранилища")

    with App(home, keyring_backend=Broken()) as app:
        rec = run(app, "login")
        assert rec.status == "ok"
        assert run_values.read(app, rec.id) == {"orders": 42, "city": "Москва"}
        assert [v["name"] for v in run_values.shown(app, rec.id)] == ["orders", "city"]
        text = (app.paths.run_dir(rec.id) / "events.jsonl").read_text(encoding="utf-8")
        assert "Секретное значение token не сохранено: нет хранилища секретов." in text
        assert "tok-XYZ-123" not in text
        assert not (app.paths.run_dir(rec.id) / "values.json").exists()


def test_collect_rejects_undeclared_and_bad(scripts):
    app = scripts
    rec = run(app, "login", mode="none")
    path = app.paths.run_dir(rec.id) / "values.json"
    path.write_text(json.dumps({"orders": "много", "extra": 1, "city": "Казань"}), encoding="utf-8")
    run_values.collect(app, rec.id)
    assert run_values.read(app, rec.id) == {"city": "Казань"}
    text = (app.paths.run_dir(rec.id) / "events.jsonl").read_text(encoding="utf-8")
    assert "Значение `extra` не объявлено в outputs, не принято." in text
    assert "Значение `orders` не принято: ожидается целое" in text
    assert not path.exists()


def test_value_refs(scripts):
    app = scripts
    src = run(app, "login")
    ref = transfer.parse_ref(f"run:{src.id}:=orders")
    assert (ref.value, ref.file, ref.text()) == ("orders", "", f"run:{src.id}:=orders")
    assert transfer.parse_ref("last:login:=orders:partial").partial
    assert transfer.parse_ref("last:login:=1bad") is None
    assert transfer.parse_ref("last:login:prices.csv").value is None

    rec = run(app, "orders", limit=f"run:{src.id}:=orders", label="last:login:=orders")
    assert rec.status == "ok" and got(app, rec) == [42, "42"]  # число в текст строкой
    assert [(e["param"], e["value"], e["run"]) for e in rec.inputs] == [("limit", "orders", src.id),
                                                                       ("label", "orders", src.id)]

    prepared = launch.prepare(app, "orders", sets=["limit=last:login:=orders"])
    assert prepared.payload()["values"]["limit"] == 42
    app.runs.finish(prepared.run.id, status="ok", reason=None)

    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "orders", values={"limit": "last:login:=city"})
    assert e.value.code == transfer.INPUT_ERROR and "ожидается целое" in e.value.message
    with pytest.raises(ReplayError) as e:
        launch.prepare(app, "orders", values={"limit": f"run:{src.id}:=nope"})
    assert "нет значения `nope`" in e.value.message


def test_preset_keeps_value_ref(scripts):
    app = scripts
    p = app.save_preset("orders", "Из входа", {"limit": "last:login:=orders"})
    assert p.results == {"limit": "last:login:=orders"} and p.missing == {}
    run(app, "login")
    prepared = launch.prepare(app, "orders", preset="Из входа")
    assert prepared.payload()["values"]["limit"] == 42
    app.runs.finish(prepared.run.id, status="ok", reason=None)
    with pytest.raises(ReplayError):
        app.save_preset("orders", "Плохой", {"limit": "текст"})


def test_lost_run_collects(scripts):
    app = scripts
    rec = run(app, "login", mode="none")
    run_values.forget(app, rec.id)
    path = app.paths.run_dir(rec.id) / "values.json"
    path.write_text(json.dumps({"token": "t-1"}), encoding="utf-8")
    run_values.collect(app, rec.id)
    assert not path.exists() and run_values.read(app, rec.id) == {"token": "t-1"}


def test_script_outputs_lists_values(scripts):
    got_ = outputs.script_outputs(scripts, "login")
    assert got_["outputs"] == []
    assert got_["values"] == [
        {"name": "token", "title": "Токен", "type": "string", "optional": False, "secret": True},
        {"name": "orders", "title": "Заказов", "type": "int", "optional": False, "secret": False},
        {"name": "city", "title": None, "type": "string", "optional": True, "secret": False}]


def test_runview_output_events(tmp_path):
    run_dir = tmp_path / "r"
    (run_dir / "output").mkdir(parents=True)
    lines = [{"t": "output", "ts": "2026-11-17T10:00:00Z", "name": "orders", "title": "Заказов", "value": 3},
             {"t": "output", "ts": "2026-11-17T10:00:01Z", "name": "token", "secret": True},
             {"t": "output", "ts": "2026-11-17T10:00:02Z", "name": "orders", "title": "Заказов", "value": 4}]
    (run_dir / "events.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    s = RunView(run_dir).snapshot()
    assert s["values"] == [{"name": "orders", "title": "Заказов", "secret": False, "value": 4},
                           {"name": "token", "title": None, "secret": True, "value": None}]
    assert [x["msg"] for x in s["log"]] == ["значение «Заказов»: 3", "значение «token»: скрыто",
                                            "значение «Заказов»: 4"]


def test_run_screen_shows_values(scripts, kr):
    from dawg_replay.web.security import Secrets
    from dawg_replay.web.server import create_app
    from starlette.testclient import TestClient

    app = scripts
    rec = run(app, "login")
    secrets_ = Secrets(port=54321)
    base = "http://127.0.0.1:54321"
    with TestClient(create_app(app.paths.home, secrets_, keyring_backend=kr), base_url=base) as client:
        client.get(f"/?token={secrets_.token}")
        html = client.get(f"/runs/{rec.id}").text
    assert "result-values" in html and "Заказов" in html and "42" in html
    assert "tok-XYZ-123" not in html


def secrets_in_keyring(kr):
    return sorted(u for (_, u) in kr.store if u.startswith("run:"))


def test_secret_values_kept_for_last_five_runs(scripts, kr):
    app = scripts
    recs = [run(app, "login") for _ in range(7)]
    assert secrets_in_keyring(kr) == sorted(f"run:{r.id}:token" for r in recs[2:])
    assert run_values.read(app, recs[0].id) == {"orders": 42, "city": "Москва"}  # несекретные остаются
    assert run_values.read(app, recs[2].id)["token"] == "tok-XYZ-123"
    assert [v["name"] for v in run_values.shown(app, recs[0].id)] == ["orders", "city"]


def add_project_run(app, prun_id, project, status, run_ids, created_at):
    app.db.execute("INSERT INTO project_runs (id, project, version, status, created_at) VALUES (?, ?, 1, ?, ?)",
                   (prun_id, project, status, created_at))
    for i, rid in enumerate(run_ids):
        app.db.execute("INSERT INTO project_steps (project_run, node, pos, status, run_id, updated_at)"
                       " VALUES (?, ?, ?, 'ok', ?, ?)", (prun_id, f"n{i}", i, rid, created_at))


def test_secret_values_kept_for_projects(scripts, kr):
    app = scripts
    recs = [run(app, "login") for _ in range(3)]
    # a: последний запуск проекта упал, его можно перезапустить с упавшего: шаг нужен
    add_project_run(app, "pa1", "a", "ok", [recs[1].id], "2026-11-17T01:00:00Z")
    add_project_run(app, "pa2", "a", "failed", [recs[0].id], "2026-11-17T02:00:00Z")
    # b: последний запуск «готово», прошлый упавший уже не перезапустить: шаг не нужен
    add_project_run(app, "pb1", "b", "failed", [recs[1].id], "2026-11-17T01:00:00Z")
    add_project_run(app, "pb2", "b", "ok", [], "2026-11-17T02:00:00Z")
    # c: запуск проекта ещё идёт
    add_project_run(app, "pc1", "c", "running", [recs[2].id], "2026-11-17T03:00:00Z")
    assert run_values.prune_secrets(app, "login", keep=0) == [recs[1].id]
    assert secrets_in_keyring(kr) == sorted([f"run:{recs[0].id}:token", f"run:{recs[2].id}:token"])
