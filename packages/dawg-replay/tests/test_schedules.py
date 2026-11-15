import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from dawg_replay import launch, schedules
from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from dawg_replay.schedules import CronTab, Schedules, Schtasks, Spec, next_run

FOREIGN = "15 3 * * * /usr/bin/backup --quiet # моё\nMAILTO=me@example.com\n"


@pytest.fixture
def crontab_file(fake_scheduler):
    return fake_scheduler / "crontab"


@pytest.fixture
def ready(app, write_script):
    app.registry.add(write_script())
    app.save_preset("ozon-cards", "daily", {"query": "чайник"})
    app.save_preset("ozon-cards", "night", {"query": "кофе"})
    return app


# --- когда запускать ---


@pytest.mark.parametrize(
    "spec, cron, text",
    [
        (Spec.daily("9:05"), "5 9 * * *", "каждый день в 09:05"),
        (Spec.weekly("mon", "18:30"), "30 18 * * 1", "по понедельникам в 18:30"),
        (Spec.weekly("вс", "07:00"), "0 7 * * 0", "по воскресеньям в 07:00"),
        (Spec.weekly("7", "07:00"), "0 7 * * 0", "по воскресеньям в 07:00"),
        (Spec.every_hours(6), "0 */6 * * *", "каждые 6 ч, с полуночи"),
        (Spec.every_hours(1), "0 * * * *", "каждый час"),
        (Spec.from_cron("*/15  9-18 * * 1-5"), "*/15 9-18 * * 1-5", "cron: */15 9-18 * * 1-5"),
    ],
)
def test_spec_to_cron_and_text(spec, cron, text):
    assert spec.cron_expr() == cron
    assert spec.describe() == text
    assert Spec.from_dict(spec.to_dict()) == spec


@pytest.mark.parametrize(
    "make",
    [
        lambda: Spec.daily("24:00"),
        lambda: Spec.daily("9"),
        lambda: Spec.weekly("пятница?", "10:00"),
        lambda: Spec.every_hours(5),
        lambda: Spec.every_hours("x"),
        lambda: Spec.from_cron("* * * *"),
        lambda: Spec.from_cron("61 * * * *"),
        lambda: Spec.from_cron("0 0 31 2 *"),  # 31 февраля не бывает
        lambda: Spec.from_cron("@daily"),
    ],
)
def test_bad_specs(make):
    with pytest.raises(ReplayError) as e:
        make()
    assert e.value.code == "bad_schedule"


def test_next_run():
    now = datetime(2026, 11, 14, 10, 30)  # суббота
    assert next_run("0 9 * * *", now) == datetime(2026, 11, 15, 9, 0)
    assert next_run("45 10 * * *", now) == datetime(2026, 11, 14, 10, 45)
    assert next_run("30 10 * * *", now) == datetime(2026, 11, 15, 10, 30)  # строго после
    assert next_run("0 */6 * * *", now) == datetime(2026, 11, 14, 12, 0)
    assert next_run("0 9 * * 1", now) == datetime(2026, 11, 16, 9, 0)  # понедельник
    assert next_run("0 9 * * mon-fri", now) == datetime(2026, 11, 16, 9, 0)
    assert next_run("0 0 1 jan *", now) == datetime(2027, 1, 1, 0, 0)
    # день месяца и день недели вместе: подходит любой (правило cron)
    assert next_run("0 9 20 * 1", now) == datetime(2026, 11, 16, 9, 0)
    assert next_run("0 0 29 2 *", now) == datetime(2028, 2, 29, 0, 0)


# --- crontab ---


def test_crontab_keeps_foreign_lines(ready, crontab_file):
    crontab_file.write_text(FOREIGN, encoding="utf-8")
    s = ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    lines = crontab_file.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == FOREIGN.splitlines()
    ours = lines[2]
    assert ours.startswith("0 9 * * * PATH=") and ours.endswith(f"# dawg-replay:{s.id}")
    args = shlex.split(ours.split(" # dawg-replay:")[0])
    assert args[0] == "0" and args[5].startswith("PATH=")
    # первым в PATH идёт каталог uv; путь сравниваем целиком, а не делим по ":" (на Windows есть "C:")
    uv_dir = str(Path(shutil.which("uv")).absolute().parent)
    assert args[5].startswith(f"PATH={uv_dir}:")
    assert args[6] == f"DAWG_REPLAY_HOME={ready.paths.home.absolute()}"
    cmd = args[7:]
    assert os.path.isabs(cmd[0])
    log = str(ready.paths.schedules / f"{s.id}.log")
    assert cmd[1:] == ["run", "ozon-cards", "--preset", "daily", "--follow", "--schedule", s.id, ">>", log, "2>&1"]

    ready.schedules.remove(s.id)
    assert crontab_file.read_text(encoding="utf-8") == FOREIGN


def test_crontab_line_escapes_percent_and_quotes(tmp_path):
    plan = schedules.TaskPlan(
        id="abcd1234", spec=Spec.daily("08:00"), argv=["/opt/my apps/dawg-replay", "run", "x", "--preset", "50%"],
        path_dirs=["/u/bin"], env={"DAWG_REPLAY_HOME": "/d/h"}, log=tmp_path / "a b.log",
    )
    line = CronTab(["true"]).line(plan)
    assert "'/opt/my apps/dawg-replay'" in line and "50\\%" in line and "%" not in line.replace("\\%", "")
    assert line.endswith("# dawg-replay:abcd1234")


def test_empty_crontab_and_commented_line(ready, crontab_file):
    assert not crontab_file.exists()
    s = ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    assert ready.schedules.list()["schedules"][0]["state"] == "ok"
    # пользователь закомментировал строку: задача не работает, list это видит
    text = crontab_file.read_text(encoding="utf-8")
    crontab_file.write_text("# " + text, encoding="utf-8")
    assert ready.schedules.list()["schedules"][0]["state"] == "missing"
    ready.schedules.remove(s.id)
    assert crontab_file.read_text(encoding="utf-8") == "# " + text


def test_missing_and_orphan_then_sync(ready, crontab_file):
    a = ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    b = ready.schedules.add("ozon-cards", "night", Spec.every_hours(12))
    lines = crontab_file.read_text(encoding="utf-8").splitlines()
    # задачу a кто-то удалил, а в crontab осталась задача удалённого из базы расписания
    orphan = lines[1].replace(b.id, "deadbeef")
    crontab_file.write_text("\n".join([lines[1], orphan]) + "\n", encoding="utf-8")
    data = ready.schedules.list()
    assert {d["id"]: d["state"] for d in data["schedules"]} == {a.id: "missing", b.id: "ok"}
    assert [o["id"] for o in data["orphans"]] == ["deadbeef"]

    r = ready.schedules.sync()
    assert r == {"reinstalled": [a.id], "orphans_removed": ["deadbeef"]}
    data = ready.schedules.list()
    assert {d["state"] for d in data["schedules"]} == {"ok"} and data["orphans"] == []


def test_remove_orphan_and_unknown(ready, crontab_file):
    crontab_file.write_text("0 9 * * * /x/dawg-replay run a --preset b --follow --schedule 0badf00d # dawg-replay:0badf00d\n", encoding="utf-8")
    assert ready.schedules.remove("0badf00d") == {"id": "0badf00d", "was_orphan": True}
    assert crontab_file.read_text(encoding="utf-8") == ""
    with pytest.raises(ReplayError) as e:
        ready.schedules.remove("0badf00d")
    assert e.value.code == "schedule_not_found"


def test_add_checks_script_and_preset(ready, crontab_file):
    with pytest.raises(ReplayError) as e:
        ready.schedules.add("nope", "daily", Spec.daily("09:00"))
    assert e.value.code == "script_not_found"
    with pytest.raises(ReplayError) as e:
        ready.schedules.add("ozon-cards", "nope", Spec.daily("09:00"))
    assert e.value.code == "preset_not_found"
    assert not crontab_file.exists() and ready.schedules.all() == []


def test_crontab_failure_leaves_nothing(ready, monkeypatch, crontab_file):
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "1")
    with pytest.raises(ReplayError) as e:
        ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    assert e.value.code == "scheduler_error" and "permission denied" in e.value.message
    assert ready.schedules.all() == []
    data = ready.schedules.list()
    assert data["error"] and data["schedules"] == []


def test_no_crontab_binary(ready, monkeypatch):
    monkeypatch.setenv("DAWG_REPLAY_CRONTAB", json.dumps(["/nonexistent/crontab-binary"]))
    with pytest.raises(ReplayError) as e:
        ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    assert e.value.code == "scheduler_unavailable"


def test_uv_not_found(ready, monkeypatch, crontab_file):
    monkeypatch.setattr(schedules.shutil, "which", lambda name: None)
    with pytest.raises(ReplayError) as e:
        ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    assert e.value.code == "uv_not_found"
    assert ready.schedules.all() == [] and not crontab_file.exists()


# --- Планировщик задач Windows (фейковый schtasks) ---


@pytest.fixture
def win(ready, monkeypatch, fake_scheduler, kr):
    monkeypatch.setenv("DAWG_REPLAY_SCHEDULER", "schtasks")
    with App(ready.paths.home, keyring_backend=kr) as a:
        yield a, fake_scheduler / "schtasks.json"


def test_schtasks_create_list_remove(win):
    app, state = win
    assert app.schedules.backend.kind == "schtasks"
    s = app.schedules.add("ozon-cards", "daily", Spec.weekly("fri", "07:15"))
    tasks = json.loads(state.read_text(encoding="utf-8"))
    task = tasks[f"dawg-replay\\{s.id}"]
    wrapper = app.paths.schedules / f"{s.id}.cmd"
    assert task["tr"] == f'"{wrapper}"'
    assert task["args"][-6:] == ["/SC", "WEEKLY", "/D", "FRI", "/ST", "07:15"]
    body = wrapper.read_bytes().decode("utf-8")
    assert "\r\n" in body and "chcp 65001" in body
    assert f'set "DAWG_REPLAY_HOME={app.paths.home.absolute()}"' in body
    assert f"--follow --schedule {s.id}" in body.replace('"', "")
    assert f'>> "{app.paths.schedules / (s.id + ".log")}" 2>&1' in body
    assert app.schedules.list()["schedules"][0]["state"] == "ok"

    app.schedules.remove(s.id)
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    assert not wrapper.exists()


def test_schtasks_hours_and_no_cron(win):
    app, state = win
    s = app.schedules.add("ozon-cards", "daily", Spec.every_hours(4))
    assert json.loads(state.read_text(encoding="utf-8"))[f"dawg-replay\\{s.id}"]["args"][-6:] == ["/SC", "HOURLY", "/MO", "4", "/ST", "00:00"]
    with pytest.raises(ReplayError) as e:
        app.schedules.add("ozon-cards", "daily", Spec.from_cron("0 9 * * *"))
    assert e.value.code == "schedule_unsupported"
    assert len(app.schedules.all()) == 1


def test_schtasks_orphan(win):
    app, state = win
    state.write_text(json.dumps({"dawg-replay\\cafebabe": {"tr": "x"}, "Other\\Task": {"tr": "y"}}), encoding="utf-8")
    data = app.schedules.list()
    assert [o["id"] for o in data["orphans"]] == ["cafebabe"]
    app.schedules.sync()
    assert list(json.loads(state.read_text(encoding="utf-8"))) == ["Other\\Task"]


def test_schtasks_wrapper_escapes_percent(tmp_path):
    plan = schedules.TaskPlan(id="abcd1234", spec=Spec.daily("08:00"), argv=["C:\\a b\\dawg-replay.exe", "run", "x", "--preset", "50%"],
                              path_dirs=["C:\\uv"], env={}, log=tmp_path / "l.log")
    body = Schtasks.wrapper(plan)
    assert ' 50%% >> ' in body and 'set "PATH=C:\\uv;%PATH%"' in body
    assert body.splitlines()[-1].startswith('"C:\\a b\\dawg-replay.exe" run x --preset 50%% >> ')


@pytest.mark.parametrize("arg, line", [
    ("macbook m5 max", '^"macbook m5 max^"'),
    ("Москва (центр)", '^"Москва ^(центр^)^"'),
    ("a&b|c<d>e^f", "a^&b^|c^<d^>e^^f"),
    ('say "hi"', '^"say \\^"hi\\^"^"'),
    ("dir\\", "dir\\"),
    ("dir \\", '^"dir \\\\^"'),
    ("", '^"^"'),
])
def test_schtasks_cmd_arg(arg, line):
    assert schedules._cmd_arg(arg) == line


WIN_NAMES = ["macbook m5 max", "Москва (центр)", '50% & "скидки" ^ <a|b>', "dir \\"]


@pytest.mark.skipif(sys.platform != "win32", reason="нужен cmd.exe")
def test_schtasks_wrapper_passes_args_through_real_cmd(tmp_path):
    echo = tmp_path / "echo.py"
    echo.write_text("import json, sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
    plan = schedules.TaskPlan(id="abcd1234", spec=Spec.daily("08:00"), argv=[sys.executable, str(echo), *WIN_NAMES],
                              path_dirs=[str(tmp_path)], env={"DAWG_REPLAY_HOME": str(tmp_path / "дом 100%")},
                              log=tmp_path / "журнал (1).log")
    wrapper = tmp_path / "w.cmd"
    wrapper.write_text(Schtasks.wrapper(plan), encoding="utf-8", newline="")
    subprocess.run(["cmd.exe", "/c", str(wrapper)], check=True, timeout=60)
    assert json.loads(plan.log.read_text(encoding="utf-8").strip().splitlines()[-1]) == WIN_NAMES


# --- удаление скрипта и пресета, плановые запуски ---


def test_remove_script_removes_schedules(ready, crontab_file):
    crontab_file.write_text(FOREIGN, encoding="utf-8")
    a = ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    b = ready.schedules.add("ozon-cards", "night", Spec.daily("23:00"))
    ready.set_secret("ozon-cards", "sheet_token", "tok")
    r = ready.remove("ozon-cards")
    assert sorted(r["schedules_removed"]) == sorted([a.id, b.id])
    assert crontab_file.read_text(encoding="utf-8") == FOREIGN and ready.schedules.all() == []


def test_remove_script_keeps_it_if_scheduler_fails(ready, monkeypatch):
    ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    monkeypatch.setenv("FAKE_CRONTAB_FAIL", "1")
    with pytest.raises(ReplayError):
        ready.remove("ozon-cards")
    assert ready.registry.get("ozon-cards") and len(ready.schedules.all()) == 1


def test_delete_preset_removes_its_schedules(ready, crontab_file):
    a = ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    b = ready.schedules.add("ozon-cards", "night", Spec.daily("23:00"))
    assert ready.delete_preset("ozon-cards", "daily") == [a.id]
    assert [s.id for s in ready.schedules.all()] == [b.id]
    assert a.id not in crontab_file.read_text(encoding="utf-8")
    with pytest.raises(ReplayError):
        ready.delete_preset("ozon-cards", "daily")


def test_scheduled_run_is_marked(ready):
    s = ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    p = launch.prepare(ready, "ozon-cards", preset="daily", schedule_id=s.id)
    run = ready.runs.get(p.run.id)
    assert (run.trigger, run.schedule_id, run.preset) == ("schedule", s.id, "daily")
    assert run.to_dict()["trigger"] == "schedule"
    manual = launch.prepare(ready, "ozon-cards", preset="daily")
    assert ready.runs.get(manual.run.id).trigger == "manual"


def test_orphan_task_does_not_run(ready):
    with pytest.raises(ReplayError) as e:
        launch.prepare(ready, "ozon-cards", preset="daily", schedule_id="deadbeef")
    assert e.value.code == "schedule_not_found" and "schedule remove deadbeef" in e.value.message
    s = ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    with pytest.raises(ReplayError) as e:
        launch.prepare(ready, "ozon-cards", preset="night", schedule_id=s.id)
    assert e.value.code == "schedule_mismatch"
    assert ready.runs.list() == []


def test_upcoming(ready):
    ready.schedules.add("ozon-cards", "daily", Spec.daily("09:00"))
    ready.schedules.add("ozon-cards", "night", Spec.every_hours(6))
    now = datetime(2026, 11, 14, 10, 30)
    up = ready.schedules.upcoming(5, now)
    assert [(u["at"], u["preset"]) for u in up] == [(datetime(2026, 11, 14, 12, 0), "night"), (datetime(2026, 11, 15, 9, 0), "daily")]


# --- CLI ---


def test_cli_schedule_flow(ready, cli, crontab_file):
    code, data = cli("schedule", "add", "ozon-cards", "--preset", "daily", "--weekly", "пн", "09:30")
    assert code == 0 and data["ok"]
    sid = data["schedule"]["id"]
    assert data["schedule"]["cron"] == "30 9 * * 1" and data["scheduler"] == "cron"
    assert f"dawg-replay:{sid}" in crontab_file.read_text(encoding="utf-8")

    code, data = cli("schedule", "list")
    assert code == 0 and [(d["id"], d["state"]) for d in data["schedules"]] == [(sid, "ok")]
    assert data["orphans"] == []

    code, data = cli("schedule", "add", "ozon-cards", "--preset", "daily", "--daily", "9:00", "--every-hours", "2")
    assert code == 1 and data["error"]["code"] == "bad_schedule"

    code, data = cli("preset", "delete", "ozon-cards", "daily")
    assert code == 0 and data["schedules_removed"] == [sid]
    code, data = cli("schedule", "list")
    assert data["schedules"] == []

    code, data = cli("schedule", "remove", sid)
    assert code == 1 and data["error"]["code"] == "schedule_not_found"


def test_cli_sync_and_text_output(ready, capsys, crontab_file):
    from dawg_replay.cli import main

    assert main(["schedule", "add", "ozon-cards", "--preset", "night", "--every-hours", "3"]) == 0
    out = capsys.readouterr().out
    assert "каждые 3 ч" in out and "снять: dawg-replay schedule remove" in out
    crontab_file.write_text("", encoding="utf-8")
    assert main(["schedule", "list"]) == 0
    assert "задача пропала" in capsys.readouterr().out
    assert main(["schedule", "sync"]) == 0
    assert "поставлено заново" in capsys.readouterr().out
