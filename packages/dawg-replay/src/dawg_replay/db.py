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
    # 5: связи между скриптами (dawg_replay.links)
    """
    -- Одна связь на пару скриптов. cond: какой исход источника подходит (ok, partial, any);
    -- starts 0: связь только передаёт файл и сама ничего не запускает.
    CREATE TABLE links (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source        TEXT NOT NULL,
        target        TEXT NOT NULL,
        cond          TEXT NOT NULL DEFAULT 'ok',
        starts        INTEGER NOT NULL DEFAULT 1,
        preset        TEXT,
        enabled       INTEGER NOT NULL DEFAULT 1,
        limit_per_day INTEGER,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (source, target)
    );
    CREATE INDEX links_by_target ON links (target);
    -- Передачи файлов: file это имя, шаблон (prices-*.csv) или @main (главный файл итога)
    CREATE TABLE link_files (
        link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
        pos     INTEGER NOT NULL,
        file    TEXT NOT NULL,
        param   TEXT NOT NULL,
        PRIMARY KEY (link_id, pos)
    );
    -- Режим получателя с несколькими источниками: all (после всех) или any (после любого)
    CREATE TABLE link_targets (
        script       TEXT PRIMARY KEY,
        mode         TEXT NOT NULL DEFAULT 'all',
        wait_seconds INTEGER NOT NULL DEFAULT 21600,
        on_timeout   TEXT NOT NULL DEFAULT 'skip'
    );
    -- Журнал решений по связи: запущен, не запущен и почему
    CREATE TABLE link_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        link_id    INTEGER NOT NULL,
        at         TEXT NOT NULL,
        decision   TEXT NOT NULL,
        text       TEXT NOT NULL,
        source_run TEXT,
        target_run TEXT
    );
    CREATE INDEX link_log_by_link ON link_log (link_id, id);
    -- Запуск по связи: triggered_by = link, какая связь и какой запуск источника его вызвал.
    -- inputs_json: откуда взяты файлы параметров (снимок для экрана запуска и повторов).
    ALTER TABLE runs ADD COLUMN link_id INTEGER;
    ALTER TABLE runs ADD COLUMN source_run TEXT;
    ALTER TABLE runs ADD COLUMN inputs_json TEXT
    """,
    # 6: автозапуск по связям (dawg_replay.chain)
    """
    -- Круг ожидания «после всех»: один открытый на получателя. arrived_json {id связи: id запуска}
    -- подходящие исходы источников, deadline срок ожидания отстающего (UTC).
    CREATE TABLE link_rounds (
        target       TEXT PRIMARY KEY,
        opened_at    TEXT NOT NULL,
        deadline     TEXT NOT NULL,
        arrived_json TEXT NOT NULL DEFAULT '{}',
        last_link    INTEGER,
        last_run     TEXT
    );
    -- Отложенный повод: получатель ещё идёт по связи, этот запуск начнётся после него.
    CREATE TABLE link_pending (
        target       TEXT PRIMARY KEY,
        link_id      INTEGER NOT NULL,
        source_run   TEXT,
        sources_json TEXT NOT NULL,
        links_json   TEXT NOT NULL,
        note         TEXT,
        created_at   TEXT NOT NULL
    )
    """,
    # 7: проекты, схемы из скриптов (dawg_replay.projects)
    """
    CREATE TABLE projects (
        name            TEXT PRIMARY KEY,
        current_version INTEGER NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    -- text: файл проекта этой версии. origin: откуда версия (file из CLI, editor с холста)
    CREATE TABLE project_versions (
        name     TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
        version  INTEGER NOT NULL,
        title    TEXT NOT NULL,
        sha256   TEXT NOT NULL,
        text     TEXT NOT NULL,
        origin   TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (name, version)
    )
    """,
    # 8: глобальные связи M6 заменены проектами: таблицы связей и автозапуска удаляются (M6 не выпущен,
    # данных у пользователей нет). Колонка runs.link_id остаётся пустой: удалять колонку в SQLite
    # умеют не все версии, которые идут с поддерживаемым Python.
    """
    DROP TABLE IF EXISTS link_pending;
    DROP TABLE IF EXISTS link_rounds;
    DROP TABLE IF EXISTS link_log;
    DROP TABLE IF EXISTS link_files;
    DROP TABLE IF EXISTS link_targets;
    DROP TABLE IF EXISTS links
    """,
    # 9: запуски проектов (dawg_replay.project_runs)
    """
    -- Запуск проекта: version какая версия файла шла, retry_of с какого запуска перезапущен.
    -- status running, ok, partial, failed, cancelled. reason у failed: timeout, lost, busy.
    -- driver_*: процесс-ведущий (следит за сроком), deadline срок по timeout_minutes (UTC).
    CREATE TABLE project_runs (
        id                  TEXT PRIMARY KEY,
        project             TEXT NOT NULL,
        version             INTEGER NOT NULL,
        status              TEXT NOT NULL,
        reason              TEXT,
        error               TEXT,
        created_at          TEXT NOT NULL,
        finished_at         TEXT,
        triggered_by        TEXT,
        schedule_id         TEXT,
        retry_of            TEXT,
        deadline            TEXT,
        cancel_requested_at TEXT,
        driver_pid          INTEGER,
        driver_started      REAL,
        heartbeat_at        TEXT,
        notify              TEXT,
        notify_state        TEXT
    );
    CREATE INDEX project_runs_by_project ON project_runs (project, created_at);
    -- Шаг: узел проекта в этом запуске. status pending, starting, running, ok, partial, failed,
    -- not_started, cancelled, skipped, off_branch. reused 1: «готово» взято из прошлого запуска.
    CREATE TABLE project_steps (
        project_run TEXT NOT NULL REFERENCES project_runs(id) ON DELETE CASCADE,
        node        TEXT NOT NULL,
        pos         INTEGER NOT NULL,
        status      TEXT NOT NULL,
        run_id      TEXT,
        reason      TEXT,
        reused      INTEGER NOT NULL DEFAULT 0,
        pid         INTEGER,
        pid_started REAL,
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (project_run, node)
    );
    -- Расписание проекта: задача в системном планировщике, как у скриптов (schedules)
    CREATE TABLE project_schedules (
        id         TEXT PRIMARY KEY,
        project    TEXT NOT NULL,
        spec_json  TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    -- Шаг проекта это обычный запуск: какой запуск проекта и какой узел
    ALTER TABLE runs ADD COLUMN project_run TEXT;
    ALTER TABLE runs ADD COLUMN project_node TEXT;
    CREATE INDEX runs_by_project_run ON runs (project_run)
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
