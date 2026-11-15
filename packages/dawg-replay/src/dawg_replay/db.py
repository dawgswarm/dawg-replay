"""SQLite: скрипты, версии, пресеты, запуски. Схема растёт миграциями по PRAGMA user_version."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: начальная схема
    """
    CREATE TABLE scripts (
        name            TEXT PRIMARY KEY,
        current_version INTEGER NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );

    CREATE TABLE script_versions (
        name          TEXT NOT NULL REFERENCES scripts(name) ON DELETE CASCADE,
        version       INTEGER NOT NULL,
        title         TEXT NOT NULL,
        description   TEXT,
        contract      INTEGER NOT NULL,
        manifest_json TEXT NOT NULL,
        sha256        TEXT NOT NULL,
        path          TEXT NOT NULL,
        added_at      TEXT NOT NULL,
        PRIMARY KEY (name, version)
    );

    CREATE TABLE presets (
        script_name TEXT NOT NULL REFERENCES scripts(name) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        params_json TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (script_name, name)
    );

    -- Заполняет раннер. Связи с scripts нет: история запусков переживает удаление скрипта,
    -- черновые запуски (run <file>) идут без регистрации.
    CREATE TABLE runs (
        id             TEXT PRIMARY KEY,
        script_name    TEXT NOT NULL,
        script_version INTEGER,
        script_path    TEXT NOT NULL,
        draft          INTEGER NOT NULL DEFAULT 0,
        preset         TEXT,
        status         TEXT NOT NULL,
        params_json    TEXT,
        created_at     TEXT NOT NULL,
        started_at     TEXT,
        finished_at    TEXT,
        exit_code      INTEGER,
        pid            INTEGER,
        summary        TEXT,
        error          TEXT
    );
    CREATE INDEX runs_by_script ON runs (script_name, created_at);
    CREATE INDEX runs_by_status ON runs (status);
    """,
    # 2: запуск через супервизор (dawg_replay.launch)
    """
    ALTER TABLE runs ADD COLUMN reason TEXT;
    ALTER TABLE runs ADD COLUMN killed INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN timeout_seconds INTEGER;
    ALTER TABLE runs ADD COLUMN supervisor_pid INTEGER;
    -- Время старта процессов по данным ОС (Unix time): отличает свой процесс от чужого с тем же pid.
    ALTER TABLE runs ADD COLUMN supervisor_started REAL;
    ALTER TABLE runs ADD COLUMN pid_started REAL;
    ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
    ALTER TABLE runs ADD COLUMN cancel_requested_at TEXT;
    ALTER TABLE runs ADD COLUMN follow INTEGER NOT NULL DEFAULT 0
    """,
    # 3: регулярные запуски через системный планировщик (dawg_replay.schedules)
    """
    CREATE TABLE schedules (
        id          TEXT PRIMARY KEY,
        script_name TEXT NOT NULL,
        preset      TEXT NOT NULL,
        spec_json   TEXT NOT NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX schedules_by_script ON schedules (script_name);
    -- Кто начал запуск: NULL вручную (UI или CLI), schedule по расписанию schedule_id
    ALTER TABLE runs ADD COLUMN triggered_by TEXT;
    ALTER TABLE runs ADD COLUMN schedule_id TEXT
    """,
    # 4: уведомления в Telegram (dawg_replay.tg_rules, dawg_replay.tg_notify)
    """
    -- Выбор на форме запуска: NULL по настройкам, always или never
    ALTER TABLE runs ADD COLUMN notify TEXT;
    -- Сообщение об итоге: NULL ещё не решено, sending, sent, failed (повторить), none (слать нечего)
    ALTER TABLE runs ADD COLUMN notify_state TEXT;
    ALTER TABLE runs ADD COLUMN notify_at TEXT;
    -- Запуски до обновления сообщений не получают
    UPDATE runs SET notify_state = 'none';
    -- Переопределения правил: owner script:<имя> или schedule:<id>, rules_json {ключ: значение}
    CREATE TABLE telegram_rules (
        owner      TEXT PRIMARY KEY,
        rules_json TEXT NOT NULL
    )
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Повторная проверка под блокировкой: другой процесс мог успеть.
            if conn.execute("PRAGMA user_version").fetchone()[0] >= i:
                conn.execute("ROLLBACK")
                continue
            for stmt in sql.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {i}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
