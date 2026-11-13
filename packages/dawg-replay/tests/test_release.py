import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fake_dawg import KEY, FakeDawg

from dawg_replay import insurance
from dawg_replay.cli import main
from dawg_replay.runs import new_run_id


@pytest.fixture
def fake():
    f = FakeDawg().start()
    yield f
    f.stop()


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@pytest.fixture
def setup(app, fake):
    """Завершённый запуск, убитый во время POST: provisioning без open, на ключе своя и чужая сессии."""
    app.secrets.set_api_key(KEY)
    app.set_config("base_url", fake.url)
    foreign = fake.add_foreign_session(iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    run_id = new_run_id()
    app.runs.reserve(
        run_id=run_id, script_name="x", script_version=1, script_path="/nope/script.py", draft=False,
        preset=None, params={}, timeout_seconds=60, limit=100,
    )
    run_dir = app.paths.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    start = datetime.now(timezone.utc)
    (run_dir / "events.jsonl").write_text(
        json.dumps({"t": "session", "ts": iso(start), "action": "provisioning"}) + "\n", encoding="utf-8"
    )
    suspect = httpx.post(f"{fake.url}/api/v1/browsers", headers={"X-API-Key": KEY}).json()["data"]["sessionId"]
    app.runs.finish(run_id, status="failed", reason="exit_code", exit_code=-9)
    report = insurance.insure(run_dir, lambda: _sessions(fake), iso(datetime.now(timezone.utc)))
    assert [s["session_id"] for s in report.suspects] == [suspect]
    return {"run_id": run_id, "suspect": suspect, "foreign": foreign, "run_dir": run_dir}


def _sessions(fake):
    from dawg_replay_kit.dawg_api import Sessions

    return Sessions(KEY, fake.url)


def test_runs_and_logs_warn(cli, setup, capsys):
    code, data = cli("runs")
    (run,) = data["runs"]
    assert [s["session_id"] for s in run["suspects"]] == [setup["suspect"]]
    code, data = cli("logs", setup["run_id"])
    assert data["suspects"][0]["session_id"] == setup["suspect"]
    code, data = cli("logs", setup["run_id"], "--events")
    assert data["suspects"][0]["session_id"] == setup["suspect"]

    assert main(["runs"]) == 0
    assert "возможно, остался браузер (1)" in capsys.readouterr().out
    for argv in (["logs", setup["run_id"]], ["logs", setup["run_id"], "--events"]):
        assert main(argv) == 0
        out = capsys.readouterr().out
        assert "возможно, остался браузер" in out and setup["suspect"] in out
        assert f"dawg-replay release {setup['run_id']} --all-suspects" in out


def test_release_all_suspects(cli, setup, fake):
    code, data = cli("release", setup["run_id"], "--all-suspects")
    assert code == 0
    assert data["results"] == [{"session_id": setup["suspect"], "released": True, "error": None}]
    assert data["suspects_left"] == []
    assert list(fake.sessions) == [setup["foreign"]]  # чужая сессия жива
    report = json.loads((setup["run_dir"] / "insurance.json").read_text(encoding="utf-8"))
    assert report["suspects"][0]["released"] is True and report["suspects"][0]["released_at"]
    events = [json.loads(x) for x in (setup["run_dir"] / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    last = events[-1]
    assert last["source"] == "dawg-replay" and "освобождена вручную" in last["msg"]
    assert not any(e.get("action") == "close" for e in events)
    # предупреждения больше нет
    assert cli("runs")[1]["runs"][0]["suspects"] == []
    code, data = cli("release", setup["run_id"], "--all-suspects")
    assert code == 1 and data["error"]["code"] == "no_suspects"


def test_release_one(cli, setup, fake):
    code, data = cli("release", setup["run_id"], setup["suspect"])
    assert code == 0 and data["results"][0]["released"]
    assert setup["suspect"] not in fake.sessions
    code, data = cli("release", setup["run_id"], setup["suspect"])
    assert code == 0 and data["results"] == []


def test_only_suspects_of_this_run(cli, setup, fake):
    code, data = cli("release", setup["run_id"], setup["foreign"])
    assert code == 1 and data["error"]["code"] == "not_a_suspect"
    assert setup["suspect"] in data["error"]["message"]
    assert setup["foreign"] in fake.sessions and setup["suspect"] in fake.sessions
    assert fake.count("DELETE") == 0


@pytest.mark.parametrize("argv", [[], ["--all-suspects", "SID"]])
def test_bad_arguments(cli, setup, argv):
    args = ["release", setup["run_id"]]
    if argv == ["--all-suspects", "SID"]:
        args += [setup["suspect"], "--all-suspects"]
    code, data = cli(*args)
    assert code == 1 and data["error"]["code"] == "bad_arguments"


def test_running_run_refused(cli, app, setup):
    app.db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (setup["run_id"],))
    code, data = cli("release", setup["run_id"], "--all-suspects")
    assert data["error"]["code"] == "run_running"


def test_no_api_key(cli, app, setup, kr):
    kr.store.pop(("dawg-replay", "dawg-api-key"))
    code, data = cli("release", setup["run_id"], "--all-suspects")
    assert data["error"]["code"] == "no_api_key"


def test_release_failure_recorded(cli, setup, fake):
    fake.delete_fails = True
    code, data = cli("release", setup["run_id"], "--all-suspects")
    assert code == 1
    assert data["results"][0]["released"] is False
    assert [s["session_id"] for s in data["suspects_left"]] == [setup["suspect"]]
    report = json.loads((setup["run_dir"] / "insurance.json").read_text(encoding="utf-8"))
    assert report["suspects"][0]["release_error"]
    fake.delete_fails = False
    code, data = cli("release", setup["run_id"], "--all-suspects")
    assert code == 0 and data["suspects_left"] == []


def test_unknown_run(cli, home):
    code, data = cli("release", "nope", "--all-suspects")
    assert code == 1
