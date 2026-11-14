"""Минуты браузеров по событиям запусков (dawg_replay.minutes) и команда `dawg-replay usage`."""

import json
from datetime import datetime, timezone

import pytest

from dawg_replay import minutes
from dawg_replay.app import App

T0 = datetime(2026, 11, 15, 10, 0, 0, tzinfo=timezone.utc)


def ts(sec: float) -> str:
    return datetime.fromtimestamp(T0.timestamp() + sec, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def session(action, sid, sec, **kw):
    return {"t": "session", "ts": ts(sec), "action": action, "session_id": sid, **kw}


def write(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        for e in events:
            f.write(e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) + "\n")


def test_open_close_pairs_and_unclosed_until_end(tmp_path):
    ev = tmp_path / "events.jsonl"
    write(
        ev,
        {"t": "session", "ts": ts(0), "action": "provisioning"},  # до open не считается
        session("open", "s1", 10, browser_id="b1"),
        {"t": "rows", "ts": ts(20), "table": "session", "rows": [{"session": 1}]},
        "битая строка \"session\"\n",
        session("close", "s1", 130),  # 2 мин
        session("open", "s2", 200),  # без close: до конца запуска
        session("close", "nope", 210),  # close без open
        session("open", "s1", 300),  # повторный open той же сессии не считается
    )
    m = minutes.count(ev, end=datetime.fromtimestamp(T0.timestamp() + 260, timezone.utc))
    assert (m.sessions, m.unclosed) == (2, 1)
    assert m.seconds == pytest.approx(120 + 60)
    assert minutes.count(ev, end=None).seconds == pytest.approx(120)  # конца нет: незакрытая не считается
    assert minutes.count(tmp_path / "missing.jsonl", end=None).sessions == 0


def test_close_from_insurance_counts_and_bad_ts_skipped(tmp_path):
    ev = tmp_path / "events.jsonl"
    write(ev, session("open", "s1", 0), {"t": "session", "ts": "вчера", "action": "close", "session_id": "s1"},
          session("close", "s1", 90, source="dawg-replay"))
    assert minutes.count(ev, end=None).seconds == pytest.approx(90)


class Rec:
    def __init__(self, finished_at=None):
        self.finished_at = finished_at


def test_for_run_caches_finished_and_recounts_after_change(tmp_path):
    ev = tmp_path / "events.jsonl"
    write(ev, session("open", "s1", 0))
    rec = Rec(finished_at=ts(60))
    assert minutes.for_run(tmp_path, rec).seconds == pytest.approx(60)
    cache = json.loads((tmp_path / minutes.CACHE_FILE).read_text(encoding="utf-8"))
    assert cache["seconds"] == pytest.approx(60)
    # Страховка дописала close позже: файл изменился, кэш пересчитывается.
    with open(ev, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(session("close", "s1", 30)) + "\n")
    assert minutes.for_run(tmp_path, rec).seconds == pytest.approx(30)
    (tmp_path / minutes.CACHE_FILE).write_text("{битый", encoding="utf-8")
    assert minutes.for_run(tmp_path, rec).seconds == pytest.approx(30)


def test_for_running_run_counts_until_now_without_cache(tmp_path):
    write(tmp_path / "events.jsonl", session("open", "s1", 0))
    now = datetime.fromtimestamp(T0.timestamp() + 600, timezone.utc)
    assert minutes.for_run(tmp_path, Rec(), now=now).minutes == pytest.approx(10)
    assert not (tmp_path / minutes.CACHE_FILE).exists()


@pytest.mark.parametrize(
    "value, text", [(0, "0 мин"), (0.05, "меньше 0,1 мин"), (2.345, "≈ 2,3 мин"), (12.6, "≈ 13 мин"), (1500, "≈ 1500 мин")]
)
def test_fmt(value, text):
    assert minutes.fmt(value) == text


def add_run(app, run_id, script, created, *events, finished=None):
    app.runs.reserve(run_id=run_id, script_name=script, script_version=1, script_path="/x/script.py", draft=False,
                     preset=None, params={}, timeout_seconds=60, limit=100)
    app.runs.finish(run_id, status="ok", reason=None, exit_code=0, summary=None)
    app.runs._db.execute("UPDATE runs SET created_at = ?, finished_at = ? WHERE id = ?", (created, finished or created, run_id))
    write(app.paths.run_dir(run_id) / "events.jsonl", *events)


@pytest.fixture
def week(home, write_script):
    with App(home) as app:
        app.registry.add(write_script(version=1))
        add_run(app, "20261115-090000-aaaaaa", "ozon-cards", ts(-3600), session("open", "a", -3600), session("close", "a", -3000))
        add_run(app, "20261114-090000-bbbbbb", "ozon-cards", ts(-86400), session("open", "b", -86400),
                finished=ts(-86400 + 300))  # без close: 5 мин по концу запуска
        add_run(app, "20261113-090000-cccccc", "other", ts(-2 * 86400), session("open", "c", -2 * 86400),
                session("close", "c", -2 * 86400 + 60))
        add_run(app, "20261113-100000-dddddd", "other", ts(-2 * 86400 + 3600))  # без браузера
        add_run(app, "20261101-090000-eeeeee", "ozon-cards", ts(-14 * 86400), session("open", "e", -14 * 86400),
                session("close", "e", -14 * 86400 + 6000))  # за пределами недели
    return home


def test_period_totals_by_script(week):
    with App(week) as app:
        p = minutes.period(app, days=7, now=T0)
        assert (p.runs, round(p.minutes, 2)) == (4, 16.0)
        assert [(s.script, round(s.minutes, 2), s.runs, s.with_browsers) for s in p.scripts] == [
            ("ozon-cards", 15.0, 2, 2), ("other", 1.0, 2, 1)]
        only = minutes.period(app, days=7, script="other", now=T0)
        assert (only.runs, round(only.minutes, 2)) == (2, 1.0)
        assert minutes.period(app, days=30, now=T0).runs == 5


def test_cli_usage(week, cli, monkeypatch):
    monkeypatch.setattr(minutes, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: T0)}))
    code, data = cli("usage")
    assert code == 0 and data["approximate"] is True and data["minutes"] == 16.0 and data["runs"] == 4
    assert [s["script"] for s in data["scripts"]] == ["ozon-cards", "other"]
    code, data = cli("usage", "--script", "ozon-cards", "--days", "30")
    assert code == 0 and data["runs"] == 3 and {r["unclosed"] for r in data["by_run"]} == {0, 1}
    code, data = cli("usage", "--script", "nope")
    assert code != 0 and data["error"]["code"] == "script_not_found"
    code, data = cli("usage", "--days", "0")
    assert code != 0 and data["error"]["code"] == "bad_value"


def test_cli_usage_text(week, capsys, monkeypatch):
    from dawg_replay.cli import main

    monkeypatch.setattr(minutes, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: T0)}))
    assert main(["usage"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Минуты браузеров DAWG за 7 дн.: ≈ 16 мин, запусков 4")
    assert "ozon-cards  ≈ 15 мин, запусков 2, с браузером 2" in out and "личном кабинете" in out
