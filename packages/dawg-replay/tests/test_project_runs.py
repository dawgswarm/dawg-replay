"""Запуск проекта (dawg_replay.project_runs): шаги по схеме, стрелки, склейка, статусы, остановка, перезапуск."""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from dawg_replay import launch
from dawg_replay import project_runs as P
from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from project_run_scripts import SOURCE, TARGET


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def steps(home, kr, monkeypatch):
    """Шаг идёт в своём потоке со своим App, как в своём процессе; поток дожидается конца шага."""
    started = []

    def spawn(app, prun_id, node):
        started.append(node)

        def work():
            with App(home, keyring_backend=kr) as a:
                P.run_step(a, prun_id, node)

        t = threading.Thread(target=work)
        t.start()
        t.join()

    monkeypatch.setattr(P, "_spawn_step", spawn)
    return started


@pytest.fixture
def scripts(app, tmp_path):
    src = tmp_path / "scripts"
    src.mkdir()
    for fname, text in (("wb.py", SOURCE.format(name="wb", title="WB")),
                        ("oz.py", SOURCE.format(name="oz", title="Ozon")),
                        ("gs.py", TARGET.format(name="gs", title="Google"))):
        (src / fname).write_text(text, encoding="utf-8")
        app.registry.add(src / fname)
    return app


def project(app, tmp_path, text, name="demo"):
    f = tmp_path / f"{name}.project.toml"
    f.write_text(text, encoding="utf-8")
    app.projects.add(f)
    return name


TWO_TO_ONE = '''\
name = "demo"
title = "Демо"

[[nodes]]
id = "wb"
script = "wb"
{wb}

[[nodes]]
id = "oz"
script = "oz"
{oz}

[[nodes]]
id = "gs"
script = "gs"
{gs}

[[edges]]
from = "wb"
to = "gs"
files = [{{ from = "cards.csv", to = "cards" }}]
{edge1}

[[edges]]
from = "oz"
to = "gs"
files = [{{ from = "cards.csv", to = "cards" }}]
{edge2}
'''


def two(app, tmp_path, wb="", oz="", gs="", edge1="", edge2=""):
    return project(app, tmp_path, TWO_TO_ONE.format(wb=wb, oz=oz, gs=gs, edge1=edge1, edge2=edge2))


def got(app, pr, node="gs"):
    run_id = pr.step(node).run_id
    return json.loads((app.paths.run_dir(run_id) / "output" / "got.json").read_text(encoding="utf-8"))


def statuses(pr):
    return {s.node: s.status for s in pr.steps}


def test_sources_merge_into_target(scripts, tmp_path, steps):
    app = scripts
    name = two(app, tmp_path, oz='values = { csv = "sku,name\\\\nB2,y" }', edge1='source_column = "Откуда"',
               edge2='source_column = "Откуда"')
    pr = P.start(app, name)
    assert pr.status == "ok" and statuses(pr) == {"wb": "ok", "oz": "ok", "gs": "ok"}
    assert steps == ["wb", "oz", "gs"]  # источники без входов первыми, получатель после обоих
    assert got(app, pr)["rows"] == [{"sku": "A1", "name": "x", "Откуда": "WB"}, {"sku": "B2", "name": "y", "Откуда": "Ozon"}]
    rec = app.runs.get(pr.step("gs").run_id)
    assert (rec.trigger, rec.project_run, rec.project_node) == ("project", pr.id, "gs")
    assert [(e["node"], e["run"]) for e in rec.inputs] == [("wb", pr.step("wb").run_id), ("oz", pr.step("oz").run_id)]
    assert [r.id for r in app.runs.of_project_run(pr.id)] == [s.run_id for s in pr.steps]


def test_partial_source_passes_by_default_and_ok_edge_skips(scripts, tmp_path, steps):
    app = scripts
    name = two(app, tmp_path, wb='values = { outcome = "partial" }')
    pr = P.start(app, name)
    assert pr.status == "partial" and statuses(pr)["gs"] == "ok"  # по умолчанию стрелка ждёт «готово или частично»
    app.projects.update(_write(tmp_path, TWO_TO_ONE.format(wb='values = { outcome = "partial" }', oz="", gs="",
                                                           edge1='when = "ok"', edge2="")))
    pr = P.start(app, name)
    assert pr.status == "partial" and statuses(pr)["gs"] == "skipped"
    assert pr.step("gs").reason == "WB закончился частично, а стрелка ждёт: готово"


def _write(tmp_path, text, name="demo"):
    f = tmp_path / f"{name}.project.toml"
    f.write_text(text, encoding="utf-8")
    return f


def test_failed_source_skips_target_with_wait_all(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, two(app, tmp_path, wb='values = { outcome = "failed" }'))
    assert pr.status == "failed" and statuses(pr) == {"wb": "failed", "oz": "ok", "gs": "skipped"}
    assert pr.step("gs").reason == "WB упал"
    assert steps == ["wb", "oz"]


def test_wait_any_runs_with_what_came(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, two(app, tmp_path, wb='values = { outcome = "failed" }', gs='wait = "any"'))
    assert statuses(pr) == {"wb": "failed", "oz": "ok", "gs": "ok"} and pr.status == "failed"
    assert got(app, pr)["rows"] == [{"sku": "A1", "name": "x"}]


def test_if_file_branches(scripts, tmp_path, steps):
    app = scripts
    text = '''\
name = "demo"
title = "Ветвление"

[[nodes]]
id = "check"
script = "wb"
values = { emit = "yes.csv" }

[[nodes]]
id = "yes"
script = "gs"

[[nodes]]
id = "no"
script = "gs"

[[nodes]]
id = "after-no"
script = "gs"

[[edges]]
from = "check"
to = "yes"
if_file = "yes.csv"
files = [{ from = "yes.csv", to = "cards" }]

[[edges]]
from = "check"
to = "no"
if_file = "no.csv"
files = [{ from = "cards.csv", to = "cards" }]

[[edges]]
from = "no"
to = "after-no"
files = [{ from = "got.json", to = "cards" }]
'''
    pr = P.start(app, project(app, tmp_path, text))
    assert statuses(pr) == {"check": "ok", "yes": "ok", "no": "off_branch", "after-no": "off_branch"}
    assert pr.status == "ok"  # «не по этой ветке» статус не портит
    assert pr.step("no").reason == "WB не отдал no.csv"


def test_columns_mismatch_means_not_started(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, two(app, tmp_path, oz='values = { csv = "sku,price\\\\nB2,10" }'))
    assert statuses(pr)["gs"] == "not_started" and pr.status == "failed"
    assert "колонки таблиц не совпадают" in pr.step("gs").reason
    rec = app.runs.get(pr.step("gs").run_id)  # причина видна и в истории скрипта
    assert rec.reason == "not_started" and rec.project_run == pr.id


def test_edge_beats_preset_and_node_values_beat_preset(scripts, tmp_path, steps):
    app = scripts
    m = app.registry.manifest("gs")
    app.presets.save("gs", "Общая", m, {"cards": [{"sku": "PRESET", "_row": 2}], "sheet": "из пресета"})
    pr = P.start(app, two(app, tmp_path, gs='preset = "Общая"\nvalues = { sheet = "узел" }'))
    assert got(app, pr) == {"rows": [{"sku": "A1", "name": "x"}, {"sku": "A1", "name": "x"}], "sheet": "узел"}


def test_wait_any_inactive_edge_leaves_param_empty(scripts, tmp_path, steps):
    app = scripts
    m = app.registry.manifest("gs")
    app.presets.save("gs", "Общая", m, {"cards": [{"sku": "PRESET", "_row": 2}]})
    text = TWO_TO_ONE.replace('[[edges]]\nfrom = "oz"\nto = "gs"\nfiles = [{{ from = "cards.csv", to = "cards" }}]\n{edge2}\n', "")
    text = text.format(wb='values = { outcome = "failed" }', oz="", gs='preset = "Общая"\nwait = "any"', edge1="",
                       edge2="")
    text += '\n[[edges]]\nfrom = "oz"\nto = "gs"\n'  # стрелка без файлов: только порядок
    pr = P.start(app, project(app, tmp_path, text))
    assert statuses(pr)["gs"] == "not_started"  # таблица пришла бы по неактивной стрелке: пуста, пресет не подставлен
    assert "cards" in pr.step("gs").reason


def test_one_run_at_a_time(scripts, tmp_path, monkeypatch):
    app = scripts
    monkeypatch.setattr(P, "_spawn_step", lambda *a: None)  # шаги «запускаются» и не идут
    name = two(app, tmp_path)
    first = P.start(app, name)
    assert first.running and statuses(first) == {"wb": "starting", "oz": "starting", "gs": "pending"}
    with pytest.raises(ReplayError) as e:
        P.start(app, name)
    assert e.value.code == "project_busy" and "Проект уже запущен" in e.value.message
    assert len(P.history(app, name)) == 1  # ручной отказ в историю не пишется
    sid = "s1"
    app.db.execute("INSERT INTO project_schedules (id, project, spec_json, created_at) VALUES (?, ?, '{}', 'now')",
                   (sid, name))
    with pytest.raises(ReplayError) as e:
        P.start(app, name, schedule_id=sid)
    skipped = P.get(app, e.value.details["run"])
    assert (skipped.status, skipped.reason, skipped.trigger) == ("failed", "busy", "schedule")
    assert skipped.label == "пропущен" and "ещё идёт" in skipped.error


def test_stop_cancels_running_and_skips_rest(scripts, tmp_path, monkeypatch):
    app = scripts

    def spawn(a, prun_id, node):  # шаг начат, его супервизора уже нет: отмена закроет запуск сама
        pr = P.get(a, prun_id)
        n = a.projects.load(pr.project).project.node(node)
        prepared = launch.prepare(a, n.script, project_run=prun_id, project_node=node)
        a.db.execute("UPDATE runs SET supervisor_pid = 999999, supervisor_started = 1 WHERE id = ?", (prepared.run.id,))
        P._set_step(a, prun_id, node, P.STEP_RUNNING, run_id=prepared.run.id)

    monkeypatch.setattr(P, "_spawn_step", spawn)
    pr = P.start(app, two(app, tmp_path))
    assert statuses(pr) == {"wb": "running", "oz": "running", "gs": "pending"}
    pr = P.stop(app, pr.id)
    assert pr.status == "cancelled" and statuses(pr) == {"wb": "cancelled", "oz": "cancelled", "gs": "skipped"}
    assert pr.step("gs").reason == "запуск проекта остановлен"
    with pytest.raises(ReplayError):
        P.stop(app, pr.id)


def test_timeout_fails_project(scripts, tmp_path, monkeypatch):
    app = scripts
    monkeypatch.setattr(P, "_spawn_step", lambda *a: None)
    pr = P.start(app, two(app, tmp_path))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    app.db.execute("UPDATE project_runs SET deadline = ? WHERE id = ?", (past, pr.id))
    app.db.execute("UPDATE project_steps SET pid = 999999, pid_started = 1 WHERE project_run = ? AND status = ?",
                   (pr.id, "starting"))
    P.advance(app, pr.id)
    pr = P.get(app, pr.id)
    assert (pr.status, pr.reason, pr.label) == ("failed", "timeout", "упал (таймаут)")
    assert statuses(pr)["gs"] == "skipped" and pr.step("gs").reason == "время запуска проекта вышло"


def test_retry_from_failed_keeps_ok_steps(scripts, tmp_path, steps):
    app = scripts
    name = two(app, tmp_path, oz='values = { outcome = "failed" }')
    first = P.start(app, name)
    assert statuses(first) == {"wb": "ok", "oz": "failed", "gs": "skipped"}
    app.projects.update(_write(tmp_path, TWO_TO_ONE.format(wb="", oz="", gs="", edge1="", edge2="")))  # новая версия
    steps.clear()
    again = P.retry(app, first.id)
    assert again.version == first.version and again.retry_of == first.id
    assert steps == ["oz"]  # «готово» не повторяется
    assert again.step("wb").reused and again.step("wb").run_id == first.step("wb").run_id
    assert statuses(again) == {"wb": "ok", "oz": "failed", "gs": "skipped"}  # та же версия: та же ошибка
    with pytest.raises(ReplayError):
        P.retry(app, "nope")
    steps.clear()
    fixed = P.start(app, name)  # «Запустить проект» берёт текущую версию
    assert fixed.version == again.version + 1 and fixed.status == "ok" and steps == ["wb", "oz", "gs"]


def test_lost_step_and_driver_recovered(scripts, tmp_path, monkeypatch):
    app = scripts

    def spawn(a, prun_id, node):
        pr = P.get(a, prun_id)
        n = a.projects.load(pr.project).project.node(node)
        prepared = launch.prepare(a, n.script, project_run=prun_id, project_node=node)
        a.db.execute("UPDATE runs SET supervisor_pid = 999999, supervisor_started = 1, heartbeat_at = '2000-01-01T00:00:00Z'"
                     " WHERE id = ?", (prepared.run.id,))

    monkeypatch.setattr(P, "_spawn_step", spawn)
    pr = P.start(app, two(app, tmp_path))
    assert pr.running
    launch.recover_lost(app)  # супервизоры шагов пропали: шаги упали, получатель пропущен, проект упал
    pr = P.get(app, pr.id)
    assert statuses(pr) == {"wb": "failed", "oz": "failed", "gs": "skipped"} and pr.status == "failed"


def test_invalid_project_does_not_start(scripts, tmp_path, steps):
    app = scripts
    name = two(app, tmp_path)
    app.registry.remove("oz")
    with pytest.raises(ReplayError) as e:
        P.start(app, name)
    assert e.value.code == "project_invalid" and P.history(app, name) == []


def test_step_label(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, two(app, tmp_path))
    assert P.step_label(app, app.runs.get(pr.step("gs").run_id)) == "Демо: Google"
    assert P.step_label(app, app.runs.get(pr.step("wb").run_id)) == "Демо: WB"


# --- Telegram ------------------------------------------------------------------------------


class Bot:
    def __init__(self):
        self.messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append(text)
        return len(self.messages)


@pytest.fixture
def tg(monkeypatch):
    from types import SimpleNamespace

    from dawg_replay import tg_notify

    bot = Bot()
    conn = SimpleNamespace(token="T0KEN", chat_id="777")
    monkeypatch.setattr(tg_notify, "_connection", lambda app: conn)
    monkeypatch.setattr(tg_notify, "_bot", lambda c: bot)
    monkeypatch.setattr(tg_notify, "_record_error", lambda app, text: None)
    return bot


def test_one_message_for_project_and_none_for_steps(scripts, tmp_path, steps, tg):
    from dawg_replay import tg_rules

    app = scripts
    tg_rules.set_general(app, "done_manual", "always")
    pr = P.start(app, two(app, tmp_path, wb='values = { outcome = "partial" }', edge1='when = "ok"'))
    assert pr.status == "partial"
    assert len(tg.messages) == 1  # шаги своих итогов не шлют
    text = tg.messages[0]
    assert text.startswith("Готово частично: Демо\n2 из 3, пропущен: Google, частично: WB\n")
    assert "пропущен: Google, WB закончился частично, а стрелка ждёт: готово" in text
    assert "В приложении: Проекты, Демо" in text
    assert P.get(app, pr.id).notify_state == "sent"


def test_project_rules_and_failed_message(scripts, tmp_path, steps, tg):
    from dawg_replay import tg_rules

    app = scripts
    name = two(app, tmp_path, oz='values = { outcome = "failed" }')
    tg_rules.set_overrides(app, "project", name, {"failed": "off"})
    P.start(app, name)
    assert tg.messages == []
    tg_rules.set_overrides(app, "project", name, {"failed": "inherit"})
    P.start(app, name)
    assert tg.messages[0].startswith("Упал: Демо\n1 из 3, упал: Ozon, пропущен: Google\n"
                                   "упал: Ozon, источник упал по просьбе теста\nпропущен: Google, Ozon упал\n")
    with pytest.raises(ReplayError) as e:
        tg_rules.set_overrides(app, "project", name, {"attach_main": "on"})
    assert "у проекта не задаётся" in e.value.message


def test_cancelled_project_sends_nothing(scripts, tmp_path, monkeypatch, tg):
    app = scripts
    monkeypatch.setattr(P, "_spawn_step", lambda *a: None)
    pr = P.start(app, two(app, tmp_path))
    app.db.execute("UPDATE project_steps SET pid = 999999, pid_started = 1 WHERE project_run = ?", (pr.id,))
    assert P.stop(app, pr.id).status == "cancelled" and tg.messages == []


# --- CLI, расписание, удаление ------------------------------------------------------------


def test_cli_run_status_runs_retry(scripts, tmp_path, steps, cli):
    name = two(scripts, tmp_path, oz='values = { outcome = "failed" }')
    code, data = cli("project", "run", name, "--follow")
    assert code == 3 and data["project_run"]["status"] == "failed"
    prun = data["project_run"]["id"]
    assert [s["status"] for s in data["project_run"]["steps"]] == ["ok", "failed", "skipped"]
    code, data = cli("project", "status", prun)
    assert code == 0 and data["project_run"]["steps"][2]["reason"] == "Ozon упал"
    code, data = cli("project", "runs", name)
    assert code == 0 and [r["id"] for r in data["runs"]] == [prun]
    from dawg_replay.cli_run import _run_line

    with App(scripts.paths.home) as a:
        rec = a.runs.list("gs", 1)[0] if a.runs.list("gs", 1) else a.runs.list("wb", 1)[0]
        assert f"[шаг проекта {prun}, узел " in _run_line(rec)
    code, data = cli("project", "retry", prun, "--follow")
    assert code == 3 and data["project_run"]["retry_of"] == prun
    code, data = cli("project", "stop", prun)
    assert code == 1 and data["error"]["code"] == "not_running"


def test_cli_busy_refusal(scripts, tmp_path, monkeypatch, cli):
    monkeypatch.setattr(P, "_spawn_step", lambda *a: None)
    name = two(scripts, tmp_path)
    code, data = cli("project", "run", name)
    assert code == 0 and data["project_run"]["status"] == "running"
    code, data = cli("project", "run", name)
    assert code == 1 and data["error"]["code"] == "project_busy"


def test_project_schedule_and_remove(scripts, tmp_path, cli, fake_scheduler):
    name = two(scripts, tmp_path)
    code, data = cli("project", "schedule", "add", name, "--daily", "07:30")
    assert code == 0, data
    sid = data["schedule"]["id"]
    crontab = (fake_scheduler / "crontab").read_text(encoding="utf-8")
    assert f"project run {name} --follow --schedule {sid}" in crontab
    code, data = cli("project", "schedule", "list")
    assert code == 0 and [s["id"] for s in data["schedules"]] == [sid] and data["schedules"][0]["state"] == "ok"
    code, data = cli("schedule", "list")
    assert code == 0 and data["orphans"] == []  # задача проекта не сирота для расписаний скриптов
    code, data = cli("telegram", "set", "done_scheduled", "off", "--schedule", sid)
    assert code == 0 and data["scope"] == "project_schedule" and data["own"] == {"done_scheduled": False}
    code, data = cli("project", "remove", name, "--yes")
    assert code == 0 and data["schedules_removed"] == [sid]
    assert sid not in (fake_scheduler / "crontab").read_text(encoding="utf-8")


def test_scheduled_run_skipped_while_busy(scripts, tmp_path, monkeypatch, cli):
    monkeypatch.setattr(P, "_spawn_step", lambda *a: None)
    name = two(scripts, tmp_path)
    code, data = cli("project", "schedule", "add", name, "--daily", "07:30")
    sid = data["schedule"]["id"]
    cli("project", "run", name)
    code, data = cli("project", "run", name, "--follow", "--schedule", sid)
    assert code == 3 and data["skipped"] and data["project_run"]["label"] == "пропущен"


def test_driver_watches_deadline_and_exits(scripts, tmp_path, monkeypatch):
    app = scripts
    monkeypatch.setattr(P, "_spawn_step", lambda *a: None)
    pr = P.start(app, two(app, tmp_path))  # шаги «запускаются»: процесс шага ещё не записался
    ticks = []

    def sleep(seconds):
        ticks.append(seconds)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
        app.db.execute("UPDATE project_runs SET deadline = ? WHERE id = ?", (past, pr.id))

    P.drive(app, pr.id, poll=0.01, sleep=sleep)
    pr = P.get(app, pr.id)
    assert ticks == [0.01, 0.01] and pr.status == "failed" and pr.reason == "timeout"  # второй круг подвёл итог
    assert {s.status for s in pr.steps} == {"cancelled", "skipped"}
