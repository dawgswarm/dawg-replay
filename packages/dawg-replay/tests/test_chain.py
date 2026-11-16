"""Автозапуск по связям (dawg_replay.chain): условия, режимы, срок ожидания, предел, отложенный повод."""

import dataclasses
import json
import sys
import threading
import time

import pytest

from dawg_replay import chain, launch, tg_notify, tg_rules
from dawg_replay.app import App
from dawg_replay.links import Transfer
from link_scripts import SOURCE, TARGET


class Spawner:
    """Вместо процесса-супервизора: запуск получателя идёт в отдельном потоке этого процесса
    (или не идёт вовсе при run=False, тогда он остаётся «идёт»)."""

    def __init__(self, home, kr, run=True):
        self.home, self.kr, self.run = home, kr, run
        self.started = []

    def __call__(self, app, prepared):
        self.started.append(prepared.run.id)
        if not self.run:
            return

        def work():
            with App(self.home, keyring_backend=self.kr) as a:
                launch.supervise(a, prepared.run.id, prepared.payload())

        t = threading.Thread(target=work)
        t.start()
        t.join()


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def spawner(home, kr, monkeypatch):
    s = Spawner(home, kr)
    monkeypatch.setattr(chain, "_spawn", s)
    return s


@pytest.fixture
def waiting(monkeypatch):
    jobs = []
    monkeypatch.setattr(chain, "_spawn_waiting", lambda app, *a: jobs.append(a))
    return jobs


@pytest.fixture
def scripts(app, tmp_path, spawner, waiting):
    for name, text in (("wb", SOURCE.format(name="wb", title="Цены WB")),
                       ("ozon", SOURCE.format(name="ozon", title="Цены Ozon")),
                       ("upd", TARGET.format(name="upd", version=1, extra=""))):
        f = tmp_path / f"{name}.py"
        f.write_text(text, encoding="utf-8")
        app.registry.add(f)
    return app


def run_source(app, name="wb", **sets):
    prepared = launch.prepare(app, name, sets=[f"{k}={v}" for k, v in sets.items()])
    return launch.supervise(app, prepared.run.id, prepared.payload())


def target_runs(app):
    """Запуски получателя в порядке создания (id внутри одной секунды случайны)."""
    ids = [r[0] for r in app.db.execute("SELECT id FROM runs WHERE script_name = 'upd' ORDER BY rowid")]
    return [app.runs.get(i) for i in ids]


def journal(app, link):
    return [(j["decision"], j["text"]) for j in reversed(app.links.journal(link.id))]


def source_log(app, run_id):
    lines = (app.paths.run_dir(run_id) / "events.jsonl").read_text("utf-8").splitlines()
    return [e["msg"] for e in map(json.loads, lines) if e.get("source") == chain.LOG_SOURCE]


def past(app, target="upd", seconds=60):
    when = chain._iso(chain._now()) if seconds == 0 else chain._iso(chain._now() - chain.timedelta(seconds=seconds))
    app.db.execute("UPDATE link_rounds SET deadline = ? WHERE target = ?", (when, target))


# --- одна связь ---


def test_success_starts_target_with_files(scripts, spawner):
    app = scripts
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    src = run_source(app, csv="sku\nA1")
    (rec,) = target_runs(app)
    assert spawner.started == [rec.id]
    assert (rec.status, rec.trigger, rec.link_id, rec.source_run) == ("ok", "link", link.id, src.id)
    assert rec.inputs[0]["run"] == src.id
    assert json.loads((app.paths.run_dir(rec.id) / "output" / "got.json").read_text("utf-8")) == ["A1"]
    assert journal(app, link) == [("started", f"запущен: {rec.id}")]
    assert source_log(app, rec.id) == [f"Запущен после: Цены WB, запуск {src.id}"]
    assert app.db.execute("SELECT COUNT(*) FROM link_rounds").fetchone()[0] == 0
    # Telegram: запуск по связи плановый, в сообщении «После: Цены WB».
    tg_rules.set_general(app, "done_scheduled", True)
    msg = tg_notify.compose(app, rec, tg_rules.plan(app, rec))
    assert msg.text.splitlines()[:2] == ["Готово: Обновить цены", "После: Цены WB"]


def test_unsuitable_outcome_in_any_mode(scripts):
    app = scripts
    app.links.set_mode("upd", "any")
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    src = run_source(app, outcome="failed")
    assert target_runs(app) == []
    assert journal(app, link) == [("unsuitable", "не запущен: Цены WB упал, нужен успешный")]
    assert source_log(app, src.id) == ["Не передал в «Обновить цены»: упал, нужен успешный"]
    run_source(app, outcome="partial")
    assert target_runs(app) == []
    app.links.update(link.id, cond="partial")
    run_source(app, outcome="partial")
    assert len(target_runs(app)) == 1


def test_any_outcome_counts_lost_and_timeout_not_cancel(scripts):
    app = scripts
    app.links.set_mode("upd", "any")
    link, _ = app.links.add("wb", "upd", cond="any")
    src = run_source(app, outcome="failed")
    assert len(target_runs(app)) == 1
    for reason, starts in (("lost", True), ("timeout", True), ("cancelled", False), ("not_started", False),
                           ("skipped", False), ("start_error", False)):
        before = len(target_runs(app))
        chain.decide(app, link, dataclasses.replace(src, status="failed", reason=reason))
        assert len(target_runs(app)) == before + int(starts), reason


def test_file_only_and_disabled_links_do_not_start(scripts):
    app = scripts
    only, _ = app.links.add("wb", "upd", starts=False, files=[Transfer("prices.csv", "items")])
    run_source(app)
    assert target_runs(app) == [] and journal(app, only) == []
    app.links.update(only.id, starts=True)
    app.links.set_enabled(only.id, False)
    src = run_source(app)
    assert target_runs(app) == []
    assert journal(app, only) == [("disabled", "не запущен: связь выключена (Цены WB закончился успешно)")]
    assert source_log(app, src.id) == ["Не передал в «Обновить цены»: связь выключена"]


def test_not_started_target_is_recorded(scripts):
    app = scripts
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    src = run_source(app, csv="code\nX")
    (rec,) = target_runs(app)
    assert (rec.status, rec.reason, rec.trigger, rec.link_id, rec.source_run) == ("failed", "not_started", "link", link.id, src.id)
    assert rec.error.startswith("Цены WB, prices.csv:")
    decision, text = journal(app, link)[0]
    assert decision == "not_started" and text.startswith("не запущен: Цены WB, prices.csv:")
    plan = tg_rules.plan(app, rec)
    assert plan.kind == "failed"
    lines = tg_notify.compose(app, rec, plan).text.splitlines()
    assert lines[:3] == ["Упал: Обновить цены", "После: Цены WB", f"Запуск по связи не начался: {rec.error.splitlines()[0]}"]


def test_cancelled_source_is_not_a_reason(scripts):
    app = scripts
    link, _ = app.links.add("wb", "upd")
    prepared = launch.prepare(app, "wb")
    app.runs.finish(prepared.run.id, status="failed", reason="cancelled")
    chain.on_finish(app, prepared.run.id)
    assert target_runs(app) == [] and journal(app, link) == []
    assert app.db.execute("SELECT COUNT(*) FROM link_rounds").fetchone()[0] == 0


# --- после всех ---


def test_all_mode_waits_for_every_source(scripts):
    app = scripts
    wb, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    oz, _ = app.links.add("ozon", "upd")
    first = run_source(app, csv="sku\nW1")
    assert target_runs(app) == []
    decision, text = journal(app, wb)[0]
    assert decision == "waiting" and text.startswith("ждёт остальных источников (Цены Ozon) до ")
    second = run_source(app, "ozon")
    (rec,) = target_runs(app)
    assert (rec.link_id, rec.source_run) == (oz.id, second.id)
    assert rec.inputs[0]["run"] == first.id
    assert journal(app, wb)[-1] == ("started", f"запущен: {rec.id}") == journal(app, oz)[-1]
    # Новый круг считается от этого запуска.
    run_source(app, "ozon")
    assert len(target_runs(app)) == 1


def test_all_mode_failed_source_waits_and_retry_counts(scripts):
    app = scripts
    wb, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    app.links.add("ozon", "upd")
    bad = run_source(app, outcome="failed")
    assert journal(app, wb)[0][1].startswith("не запущен: Цены WB упал, нужен успешный; круг ждёт до ")
    assert source_log(app, bad.id) == ["Не передал в «Обновить цены»: упал, нужен успешный"]
    run_source(app, "ozon")
    assert target_runs(app) == []
    good = run_source(app, csv="sku\nRETRY")
    (rec,) = target_runs(app)
    assert rec.inputs[0]["run"] == good.id


def test_all_mode_timeout_skip(scripts):
    app = scripts
    wb, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    oz, _ = app.links.add("ozon", "upd")
    src = run_source(app)
    past(app)
    assert chain.tick(app) == ["upd"]
    (rec,) = target_runs(app)
    assert (rec.status, rec.reason, rec.trigger, rec.link_id, rec.source_run) == ("failed", "skipped", "link", wb.id, src.id)
    assert rec.error.startswith("не дождался источников (Цены Ozon): срок ожидания вышел в ")
    assert "компьютер был выключен" not in rec.error
    assert journal(app, oz)[-1][0] == "skipped" and journal(app, wb)[-1][0] == "skipped"
    assert tg_rules.plan(app, rec).kind == "failed"
    assert tg_notify.failure_reason(app, rec, None).startswith("Запуск по связи не начался: не дождался")
    assert chain.tick(app) == []


def test_late_deadline_after_computer_was_off(scripts):
    app = scripts
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    app.links.add("ozon", "upd")
    run_source(app)
    past(app, seconds=3600)
    # Первый удобный случай: любой запуск (recover_lost) обрабатывает срок с отметкой об опоздании.
    launch.recover_lost(app)
    (rec,) = target_runs(app)
    assert rec.reason == "skipped" and rec.error.endswith("обработан позже: компьютер был выключен")


def test_timeout_run_with_what_is_there(scripts):
    app = scripts
    wb, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    oz, _ = app.links.add("ozon", "upd")
    app.links.set_mode("upd", on_timeout="run")
    src = run_source(app, csv="sku\nLATE")
    past(app, seconds=3600)
    chain.tick(app)
    (rec,) = target_runs(app)
    assert rec.status == "ok" and rec.inputs[0]["run"] == src.id
    log = source_log(app, rec.id)
    assert log[0] == f"Запущен после: Цены WB, запуск {src.id}"
    assert log[1].startswith("Срок ожидания вышел в ") and log[1].endswith("запущен позже: компьютер был выключен.")
    assert journal(app, oz)[-2][1].endswith("запуск с тем, что есть")


def test_arrival_after_expired_deadline_opens_new_round(scripts):
    app = scripts
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    app.links.add("ozon", "upd")
    run_source(app)
    past(app)
    run_source(app, "ozon")  # сначала закрывается старый круг (пропуск), потом ozon открывает новый
    (rec,) = target_runs(app)
    assert rec.reason == "skipped"
    row = app.db.execute("SELECT arrived_json FROM link_rounds WHERE target = 'upd'").fetchone()
    assert list(json.loads(row[0]).values()) != []


# --- предел, отложенный повод, лимит одновременных ---


def test_limit_per_day(scripts):
    app = scripts
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")], limit_per_day=1)
    run_source(app)
    src = run_source(app)
    assert len(target_runs(app)) == 1
    assert journal(app, link)[-1] == ("limit", "не запущен: исчерпан предел 1 в сутки")
    assert source_log(app, src.id) == ["Не передал в «Обновить цены»: исчерпан предел 1 запусков в сутки"]
    assert chain.started_today(app, link.id) == 1


def test_cycle_with_limit_stops(scripts):
    app = scripts
    a, _ = app.links.add("wb", "ozon")
    b, _ = app.links.add("ozon", "wb", limit_per_day=2)
    run_source(app)
    # wb → ozon → wb → ozon → wb → ozon, дальше предел связи ozon → wb.
    assert len(app.runs.list("wb")) == 3 and len(app.runs.list("ozon")) == 3
    assert journal(app, b)[-1][0] == "limit"


def test_busy_target_defers_one_and_merges_rest(scripts, spawner):
    app = scripts
    spawner.run = False
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    run_source(app, csv="sku\nA")
    (first,) = target_runs(app)
    assert first.running
    run_source(app, csv="sku\nB")
    last = run_source(app, csv="sku\nC")
    assert [d for d, _ in journal(app, link)] == ["started", "deferred", "merged"]
    assert len(target_runs(app)) == 1
    spawner.run = True
    app.runs.finish(first.id, status="ok", reason=None)
    chain.on_finish(app, first.id)
    runs = target_runs(app)
    assert len(runs) == 2 and runs[1].source_run == last.id and runs[1].status == "ok"
    assert app.db.execute("SELECT COUNT(*) FROM link_pending").fetchone()[0] == 0


def test_pending_starts_from_tick_if_target_closed_elsewhere(scripts, spawner):
    app = scripts
    spawner.run = False
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    run_source(app)
    run_source(app)
    (first,) = target_runs(app)
    app.runs.finish(first.id, status="failed", reason="lost")  # закрыл кто-то без on_finish
    spawner.run = True
    assert chain.tick(app) == ["upd"]
    assert len(target_runs(app)) == 2


def test_parallel_limit_hands_over_to_waiting_process(scripts, waiting):
    app = scripts
    link, _ = app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    app.set_config("max_parallel_runs", "1")
    src = run_source(app)
    assert len(target_runs(app)) == 1  # источник уже закрыт: место свободно
    other = launch.prepare(app, "ozon")  # место занято запуском другого скрипта
    chain.decide(app, link, src)
    assert len(waiting) == 1 and journal(app, link)[-1][0] == "queued"
    assert len(target_runs(app)) == 1
    target, links, sources, lk, source_run, note = waiting[0]
    app.runs.finish(other.run.id, status="ok", reason=None)
    job = {"target": target, "links": [x.id for x in links], "sources": {str(k): v for k, v in sources.items()},
           "link": lk.id, "source_run": source_run, "note": note}
    rec = chain.run_waiting(app, job)
    assert (rec.status, rec.trigger, rec.source_run) == ("ok", "link", src.id)
    assert journal(app, link)[-1] == ("started", f"запущен: {rec.id}")


# --- процесс ожидания сроков ---


def test_wait_loop_fires_deadline_and_exits(scripts, home):
    app = scripts
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    app.links.add("ozon", "upd")
    run_source(app)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        with App(home) as a:
            past(a)

    chain.wait_loop(home, sleep=sleep)
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= chain.WAITER_POLL_SECONDS
    (rec,) = target_runs(app)
    assert rec.reason == "skipped"
    assert not (home / chain.WAITER_FILE).exists()


def test_ensure_waiter(scripts, monkeypatch):
    app = scripts
    calls = []
    monkeypatch.setattr(chain, "_spawn_detached", lambda app, args, log: calls.append(args))
    chain.ensure_waiter(app)
    assert calls == []  # в тестах выключен
    monkeypatch.setenv(chain.ENV_WAITER, "")
    chain.ensure_waiter(app)
    assert calls == [["wait"]]
    import os

    from dawg_replay import procinfo

    (app.paths.home / chain.WAITER_FILE).write_text(
        json.dumps({"pid": os.getpid(), "started": procinfo.start_time(os.getpid())}), "utf-8")
    chain.ensure_waiter(app)
    assert calls == [["wait"]]  # уже живёт


def test_real_supervisor_process(scripts, monkeypatch):
    """Получатель запускается отдельным процессом-супервизором, как плановый."""
    app = scripts
    monkeypatch.setattr(chain, "_spawn", chain._spawn_supervisor)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    app.links.add("wb", "upd", files=[Transfer("prices.csv", "items")])
    run_source(app, csv="sku\nREAL")
    (rec,) = target_runs(app)
    for _ in range(600):
        rec = app.runs.get(rec.id)
        if not rec.running:
            break
        time.sleep(0.05)
    assert rec.status == "ok" and rec.trigger == "link"
    assert json.loads((app.paths.run_dir(rec.id) / "output" / "got.json").read_text("utf-8")) == ["REAL"]
