"""Правила уведомлений в Telegram и сообщение об итоге. Сеть не нужна: Bot подменён фейком."""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dawg_replay import cli_telegram_rules, launch, tg_notify, tg_rules
from dawg_replay.config import load_config, set_config_value
from dawg_replay.db import connect
from dawg_replay.errors import ReplayError
from dawg_replay.runs import new_run_id
from dawg_replay.schedules import Spec

TOKEN = "123456:SECRET-bot-token"

SCRIPT = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2"]
# ///

# /// dawg-script
# contract = 1
# name = "{name}"
# title = "Карточки WB"
# version = 1
#
# [[params]]
# name = "mode"
# type = "string"
# title = "Режим"
# default = "ok"
#
# [[params]]
# name = "token"
# type = "secret"
# title = "Токен"
# required = false
# ///
import sys
import dawg_replay_kit as kit

mode = kit.params.mode
if mode == "fail":
    kit.log.error("сайт не открылся, токен " + str(kit.params.token))
    sys.exit(2)
(kit.output_dir / "report.csv").write_text("a,b\\n1,2\\n", encoding="utf-8")
kit.item("r1", "ok")
kit.item("r2", "failed", "нет карточки")
kit.result("partial" if mode == "partial" else "ok", "Собрано 1 из 2, токен " + str(kit.params.token),
           count=1, links=[{{"title": "Таблица", "url": "https://example.com/sheet"}}], main="report.csv")
'''


class FakeBot:
    def __init__(self, fail: int = 0, code: int | None = None):
        self.messages: list[tuple[str, str]] = []
        self.documents: list[tuple[str, Path]] = []
        self.fail = fail
        self.code = code
        self.lock = threading.Lock()

    def send_message(self, chat_id, text, reply_markup=None):
        with self.lock:
            if self.fail:
                self.fail -= 1
                err = RuntimeError(f"сеть недоступна https://api.telegram.org/bot{TOKEN}/sendMessage")
                err.code = self.code
                raise err
            self.messages.append((chat_id, text))
            return len(self.messages)

    def send_document(self, chat_id, path, caption=None):
        with self.lock:
            self.documents.append((chat_id, Path(path)))
            return 100


@pytest.fixture
def tg(monkeypatch):
    """Подключённый Telegram на фейке: бот, ошибки и маскировка токена."""
    state = SimpleNamespace(bot=FakeBot(), connected=True, errors=[])
    conn = SimpleNamespace(token=TOKEN, chat_id="777", api_url="https://api.telegram.org", chat_name="я", bot_name="b")
    monkeypatch.setattr(tg_notify, "_connection", lambda app: conn if state.connected else None)
    monkeypatch.setattr(tg_notify, "_bot", lambda c: state.bot)
    monkeypatch.setattr(tg_notify, "_record_error", lambda app, text: state.errors.append(text))
    monkeypatch.setattr(tg_notify, "_error_text", lambda e, c: str(e).replace(c.token, "***"))
    monkeypatch.setattr(tg_notify, "RETRY_PAUSE", 0.01)
    monkeypatch.setattr(tg_rules, "connected", lambda app: state.connected)
    return state


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def wb(app, tmp_path, kr):
    f = tmp_path / "src" / "wb.py"
    f.parent.mkdir(exist_ok=True)
    f.write_text(SCRIPT.format(name="wb-cards"), encoding="utf-8")
    app.registry.add(f)
    kr.set_password("dawg-replay", "secret:wb-cards:token", "tok-VERY-SECRET")
    return "wb-cards"


def run_follow(app, name, **kw):
    prepared = launch.prepare(app, name, **kw)
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    for t in threading.enumerate():
        if t.name.startswith("dawg-tg-"):
            t.join(10)
    return app.runs.get(rec.id)


def fake_run(app, *, status="ok", reason=None, trigger=None, minutes=1.0, draft=False, notify=None,
             finished_ago=timedelta(0), summary=None, error=None, script="wb-cards"):
    """Закрытый запуск без процесса: для правил и текстов."""
    run_id = new_run_id()
    end = datetime.now(timezone.utc) - finished_ago
    start = end - timedelta(minutes=minutes)
    iso = lambda d: d.isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
    app.db.execute(
        "INSERT INTO runs (id, script_name, script_version, script_path, draft, status, reason, params_json, created_at,"
        " started_at, finished_at, summary, error, triggered_by, schedule_id, notify)"
        " VALUES (?, ?, 1, '/x.py', ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, script, int(draft), status, reason, iso(start), iso(start), iso(end), summary, error,
         "schedule" if trigger else None, trigger, notify),
    )
    return app.runs.get(run_id)


# --- настройки и уровни ----------------------------------------------------------------


def test_config_bool_and_choice(home, app):
    cfg = app.config
    assert (cfg.telegram_failed, cfg.telegram_done_manual, cfg.telegram_long_minutes, cfg.telegram_attach_main) == (
        True, "long", 10, False)
    set_config_value(app.paths.config, "telegram_failed", "off")
    set_config_value(app.paths.config, "telegram_done_manual", "never")
    text = app.paths.config.read_text(encoding="utf-8")
    assert "telegram_failed = false" in text and 'telegram_done_manual = "never"' in text
    assert load_config(app.paths.config).telegram_failed is False
    with pytest.raises(ReplayError, match="on или off"):
        set_config_value(app.paths.config, "telegram_ask", "может")
    with pytest.raises(ReplayError, match="always, long, never"):
        set_config_value(app.paths.config, "telegram_done_manual", "sometimes")
    with pytest.raises(ReplayError, match="от 1 до 1440"):
        set_config_value(app.paths.config, "telegram_long_minutes", "5000")
    # испорченное значение в файле не ломает запуск: берётся значение по умолчанию
    app.paths.config.write_text('telegram_done_manual = "x"\ntelegram_long_minutes = 99999\n', encoding="utf-8")
    cfg = load_config(app.paths.config)
    assert (cfg.telegram_done_manual, cfg.telegram_long_minutes) == ("long", 1440)


def test_layers_script_then_schedule(app, wb):
    app.save_preset(wb, "p", {})
    s = app.schedules.add(wb, "p", Spec.daily("09:00"))
    tg_rules.set_general(app, "failed", "off")
    assert tg_rules.effective(app, wb)["failed"] is False
    tg_rules.set_overrides(app, "script", wb, {"failed": "on", "long_minutes": "3"})
    assert tg_rules.effective(app, wb)["failed"] is True
    assert tg_rules.effective(app, wb)["long_minutes"] == 3
    tg_rules.set_overrides(app, "schedule", s.id, {"failed": "off", "attach_main": "on"})
    eff = tg_rules.effective(app, wb, s.id)
    assert (eff["failed"], eff["attach_main"], eff["long_minutes"]) == (False, True, 3)
    with pytest.raises(ReplayError, match="у расписания не задаётся"):
        tg_rules.set_overrides(app, "schedule", s.id, {"done_manual": "always"})
    # inherit снимает только этот ключ
    assert tg_rules.set_overrides(app, "script", wb, {"failed": "inherit"}) == {"long_minutes": 3}
    assert tg_rules.effective(app, wb)["failed"] is False
    rows = {r["key"]: r for r in tg_rules.layers(app, "schedule", s.id, wb)}
    assert rows["failed"]["own"] is False and rows["ask"]["own"] is None and "done_manual" not in rows
    # снятое расписание и удалённый скрипт уносят свои правила
    app.schedules.remove(s.id)
    assert tg_rules.overrides(app, "schedule", s.id) == {}
    app.remove(wb)
    assert app.db.execute("SELECT COUNT(*) FROM telegram_rules").fetchone()[0] == 0


def test_plan_rules(app, wb):
    plan = lambda rec, sus=None: tg_rules.plan(app, rec, sus)  # noqa: E731
    assert plan(fake_run(app, status="failed", reason="timeout")).kind == "failed"
    assert plan(fake_run(app, status="failed", reason="cancelled")).kind is None
    assert plan(fake_run(app, status="failed", draft=True)).kind is None
    # ручной: по умолчанию только если дольше 10 минут
    assert plan(fake_run(app, minutes=3)).kind is None
    assert plan(fake_run(app, minutes=11)).kind == "done"
    assert plan(fake_run(app, minutes=3, notify="always")).kind == "done"
    assert plan(fake_run(app, minutes=60, notify="never")).kind is None
    # выбор у запуска падения не касается
    assert plan(fake_run(app, status="failed", notify="never")).kind == "failed"
    # по расписанию
    assert plan(fake_run(app, minutes=1, trigger="abc")).kind == "done"
    tg_rules.set_overrides(app, "script", wb, {"done_scheduled": "off"})
    assert plan(fake_run(app, minutes=1, trigger="abc")).kind is None
    # тихая поломка пишет и без сообщения об итоге, но не при «нет» у запуска
    sus = {"kind": "empty", "text": "ничего не собрал"}
    assert plan(fake_run(app, minutes=1), sus) == tg_rules.Plan("done", sus, False)
    assert plan(fake_run(app, minutes=1, notify="never"), sus).kind is None
    tg_rules.set_general(app, "suspicious", "off")
    assert plan(fake_run(app, minutes=1), sus).kind is None
    assert plan(fake_run(app, minutes=11), sus).suspicion is None
    tg_rules.set_general(app, "done_manual", "never")
    assert tg_rules.manual_text(tg_rules.effective(app, wb)) == "только если упадёт"


def test_wants_ask(app, wb):
    rec = fake_run(app)
    assert tg_rules.wants_ask(app, rec) is True
    tg_rules.set_overrides(app, "script", wb, {"ask": "off"})
    assert tg_rules.wants_ask(app, rec) is False
    assert tg_rules.wants_ask(app, fake_run(app, draft=True, script="other")) is False


# --- сообщение об итоге ----------------------------------------------------------------


def test_done_message_after_real_run(app, wb, tg):
    tg_rules.set_general(app, "attach_main", "on")
    rec = run_follow(app, wb, notify="always")
    assert rec.status == "ok" and rec.notify == "always"
    assert app.runs.get(rec.id).notify_state == "sent"
    ((chat, text),) = tg.bot.messages
    assert chat == "777"
    assert text.startswith("Готово: Карточки WB\n")
    assert "Собрано 1 из 2" in text and "Строки: 1 из 2, 1 упала" in text and "Собрано записей: 1" in text
    assert "Таблица: https://example.com/sheet" in text
    assert "tok-VERY-SECRET" not in text and "В приложении: Запуски, Карточки WB, сегодня" in text
    assert [p.name for _, p in tg.bot.documents] == ["report.csv"]
    log = (app.paths.run_dir(rec.id) / "events.jsonl").read_text(encoding="utf-8")
    assert "Сообщение об итоге отправлено в Telegram" in log
    # повторный вызов ничего не шлёт: запуск уже отмечен
    assert tg_notify.after_finish(app, rec.id, background=False) is None
    assert len(tg.bot.messages) == 1


def test_short_manual_run_is_quiet_and_failure_is_not(app, wb, tg):
    rec = run_follow(app, wb)
    assert tg.bot.messages == [] and app.runs.get(rec.id).notify_state == "none"
    rec = run_follow(app, wb, sets=["mode=fail"], notify="never")
    assert rec.status == "failed"
    ((_, text),) = tg.bot.messages
    assert text.startswith("Упал: Карточки WB\n")
    assert "сайт не открылся" in text and "tok-VERY-SECRET" not in text


def test_partial_and_preset_title(app, wb, tg):
    app.save_preset(wb, "ночь", {"mode": "partial"})
    rec = run_follow(app, wb, preset="ночь", notify="always")
    assert rec.status == "partial"
    assert tg.bot.messages[0][1].startswith("Готово частично: Карточки WB, пресет «ночь»")


def test_big_main_file_is_only_mentioned(app, wb, tg, monkeypatch):
    monkeypatch.setattr(tg_notify, "MAX_DOCUMENT", 3)
    tg_rules.set_general(app, "attach_main", "on")
    run_follow(app, wb, notify="always")
    assert tg.bot.documents == []
    assert "Файл report.csv больше 50 МБ" in tg.bot.messages[0][1]


def test_not_connected_marks_none(app, wb, tg):
    tg.connected = False
    rec = run_follow(app, wb, notify="always")
    assert app.runs.get(rec.id).notify_state == "none" and tg.bot.messages == []


def test_failure_is_logged_and_retried_by_next_process(app, wb, tg):
    tg.bot.fail = 10
    rec = run_follow(app, wb, notify="always")
    assert rec.status == "ok"  # Telegram на итог не влияет
    assert app.runs.get(rec.id).notify_state == "failed"
    assert len(tg.errors) == tg_notify.BACKGROUND_ATTEMPTS and TOKEN not in "".join(tg.errors)
    log = (app.paths.run_dir(rec.id) / "events.jsonl").read_text(encoding="utf-8")
    assert "Сообщение об итоге в Telegram не отправлено" in log and TOKEN not in log
    tg.bot.fail = 0
    launch.recover_lost(app)  # любая следующая команда
    assert app.runs.get(rec.id).notify_state == "sent" and len(tg.bot.messages) == 1
    launch.recover_lost(app)
    assert len(tg.bot.messages) == 1


def test_permanent_error_is_not_retried_in_place(app, wb, tg):
    tg.bot.fail, tg.bot.code = 10, 403
    run_follow(app, wb, notify="always")
    assert len(tg.errors) == 1


def test_retry_rules_age_and_claims(app, wb, tg):
    old = fake_run(app, status="failed", reason="timeout", finished_ago=timedelta(days=2))
    fresh_unclaimed = fake_run(app, status="failed", reason="timeout", finished_ago=timedelta(minutes=5))
    just_finished = fake_run(app, status="failed", reason="timeout")  # его ещё шлёт свой процесс
    stuck = fake_run(app, status="failed", reason="timeout", finished_ago=timedelta(hours=1))
    app.db.execute("UPDATE runs SET notify_state = 'sending', notify_at = '2020-01-01T00:00:00Z' WHERE id = ?", (stuck.id,))
    sent = tg_notify.retry_pending(app)
    assert set(sent) == {fresh_unclaimed.id, stuck.id}
    assert app.runs.get(old.id).notify_state is None and app.runs.get(just_finished.id).notify_state is None


def test_one_claim_wins_between_processes(app, wb, tg):
    rec = fake_run(app, status="failed", reason="timeout")
    other = connect(app.paths.db)
    try:
        assert tg_notify._claim(app.db, rec.id) is True
        assert tg_notify._claim(other, rec.id) is False
    finally:
        other.close()


def test_lost_run_is_sent_by_closer(app, wb, tg):
    run_id = new_run_id()
    app.runs.reserve(run_id=run_id, script_name=wb, script_version=1, script_path="/x.py", draft=False, preset=None,
                     params={}, timeout_seconds=60, limit=10)
    app.db.execute("UPDATE runs SET heartbeat_at = '2020-01-01T00:00:00Z', supervisor_pid = NULL WHERE id = ?", (run_id,))
    launch.recover_lost(app)
    assert app.runs.get(run_id).label == "failed (lost)"
    ((_, text),) = tg.bot.messages
    assert "Упал: Карточки WB" in text and "программа, которая его вела, закрылась" in text


def test_not_started_schedule_is_sent(app, wb, tg):
    rec = launch.record_not_started(app, "abcd", wb, None, ReplayError("bad_params", "нет обязательной таблицы «Артикулы»"))
    assert app.runs.get(rec.id).notify_state == "sent"
    assert "Плановый запуск не начался: нет обязательной таблицы «Артикулы»" in tg.bot.messages[0][1]


def test_supervise_does_not_wait_for_telegram(app, wb, tg, monkeypatch):
    gate = threading.Event()
    slow = tg.bot.send_message

    def blocked(*a, **kw):
        gate.wait(10)
        return slow(*a, **kw)

    monkeypatch.setattr(tg.bot, "send_message", blocked)
    prepared = launch.prepare(app, wb, notify="always")
    t0 = time.monotonic()
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    assert rec.status == "ok" and time.monotonic() - t0 < 8
    assert app.runs.get(rec.id).notify_state == "sending"
    gate.set()
    for t in threading.enumerate():
        if t.name.startswith("dawg-tg-"):
            t.join(10)
    assert app.runs.get(rec.id).notify_state == "sent"


def test_migration_marks_old_runs(tmp_path):
    import sqlite3

    from dawg_replay import db

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for i, sql in enumerate(db.MIGRATIONS[:3], start=1):
        for stmt in sql.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {i}")
    conn.execute("INSERT INTO runs (id, script_name, script_path, status, created_at, finished_at) VALUES"
                 " ('r1', 's', '/x', 'failed', '2026-01-01', '2026-01-01')")
    conn.close()
    new = connect(path)
    try:
        assert tuple(new.execute("SELECT notify_state, notify FROM runs").fetchone()) == ("none", None)
    finally:
        new.close()


def test_duration_text():
    assert [tg_notify.duration_text(s) for s in (5, 600, 3600, 3900)] == ["5 с", "10 мин", "1 ч", "1 ч 5 мин"]


# --- CLI -----------------------------------------------------------------------------


def run_set(*argv):
    parser = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    tg_sub = parser.add_subparsers(dest="cmd")
    cli_telegram_rules.add_set(tg_sub, common)
    args = parser.parse_args(["set", "--json", *argv])
    return args.func(args)


def test_cli_set(home, app, wb, capsys):
    assert run_set("failed", "off") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rules"]["failed"] is False and out["scope"] == "general"
    assert run_set("long_minutes", "25", "--script", wb) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["own"] == {"long_minutes": 25} and out["rules"]["failed"] is False
    assert run_set("done_manual", "always", "--schedule", "nope") == 1
    assert "nope" in capsys.readouterr().out
    assert run_set("failed", "inherit") == 1
    assert "только у скрипта" in capsys.readouterr().out
    assert run_set() == 0
    assert json.loads(capsys.readouterr().out)["rules"]["long_minutes"] == 10


def test_cli_run_notify_flags(cli, app, wb, tg):
    code, data = cli("run", wb, "--follow", "--no-notify")
    assert code == 0 and data["run"]["notify"] == "never"
    code, data = cli("run", wb, "--follow", "--notify")
    for t in threading.enumerate():
        if t.name.startswith("dawg-tg-"):
            t.join(10)
    assert code == 0 and data["run"]["notify"] == "always"
    assert len(tg.bot.messages) == 1


def test_telegram_set_is_in_main_cli(cli, home):
    code, data = cli("telegram", "set", "attach_main", "on")
    assert code == 0 and data["rules"]["attach_main"] is True


def test_real_connection_feeds_bot(app, wb, monkeypatch):
    """Без подмены _connection: токен из хранилища паролей, чат из config, запрос уходит в Bot."""
    from dawg_replay import telegram

    sent = []
    monkeypatch.setattr(telegram.Bot, "send_message", lambda self, chat, text, reply_markup=None: sent.append((self.token, chat)) or 1)
    app.secrets.set_telegram_token(TOKEN)
    app.set_config("telegram_chat_id", "555")
    rec = fake_run(app, status="failed", reason="timeout")
    assert tg_notify.after_finish(app, rec.id, background=False) is True
    assert sent == [(TOKEN, "555")]


def test_tg_asks_uses_rules(app, wb):
    from dawg_replay import tg_asks

    rec = fake_run(app)
    assert tg_asks.wants_ask(app, rec) is True
    tg_rules.set_overrides(app, "script", wb, {"ask": "off"})
    assert tg_asks.wants_ask(app, rec) is False
