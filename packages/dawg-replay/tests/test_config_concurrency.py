"""config.toml при параллельном чтении и записи (dr-109).

На Windows файл, который заменяют, коротко не открывается (PermissionError), а файл,
открытый на чтение, нельзя заменить. Плюс «прочитать, поменять, записать» из разных
потоков и процессов не должно терять изменения.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from dawg_replay import config
from dawg_replay.config import Config, load_config, save_config, update_config


def flaky(real, failures):
    """Обёртка: первые `failures` вызовов дают PermissionError, как Windows во время замены."""
    left = [failures]

    def call(*a, **k):
        if left[0] > 0:
            left[0] -= 1
            raise PermissionError(13, "Permission denied")
        return real(*a, **k)

    return call


def test_windows_read_and_replace_retried(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = home / "config.toml"
    save_config(path, Config(telegram_bot="bot_one"))
    monkeypatch.setattr(config, "_IS_WINDOWS", True)
    monkeypatch.setattr(config, "_RETRY_DELAYS", (0.001,) * 5)
    monkeypatch.setattr(Path, "read_text", flaky(Path.read_text, 3))
    assert load_config(path).telegram_bot == "bot_one"
    monkeypatch.setattr(os, "replace", flaky(os.replace, 3))
    update_config(path, {"telegram_bot": "bot_two"})
    assert load_config(path).telegram_bot == "bot_two"
    assert sorted(p.name for p in home.iterdir()) == ["config.toml", "config.toml.lock"]  # без временных


def test_windows_gives_up_after_retries(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    save_config(path, Config())
    monkeypatch.setattr(config, "_IS_WINDOWS", True)
    monkeypatch.setattr(config, "_RETRY_DELAYS", (0.001,) * 3)
    monkeypatch.setattr(Path, "read_text", flaky(Path.read_text, 100))
    with pytest.raises(PermissionError):
        load_config(path)


def test_permission_error_not_hidden_elsewhere(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    save_config(path, Config())
    monkeypatch.setattr(config, "_IS_WINDOWS", False)
    monkeypatch.setattr(Path, "read_text", flaky(Path.read_text, 1))
    with pytest.raises(PermissionError):  # не Windows: настоящая ошибка прав, не повторяем
        load_config(path)


def test_missing_file_is_default(tmp_path):
    assert load_config(tmp_path / "нет.toml") == Config()


def test_threads_read_while_others_write_and_nothing_lost(tmp_path):
    """Как экран Настроек (опрос каждые 2 с) рядом с потоком привязки Telegram, только плотнее."""
    path = tmp_path / "config.toml"
    save_config(path, Config())
    errors: list[BaseException] = []
    stop = threading.Event()
    keys = ("telegram_chat_id", "telegram_chat_name", "telegram_bot")

    def writer(key):
        try:
            for i in range(60):
                update_config(path, {key: f"{key}-{i}"})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            while not stop.is_set():
                load_config(path)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(k,)) for k in keys]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(60)
    stop.set()
    for t in readers:
        t.join(10)
    assert errors == []
    cfg = load_config(path)
    assert [getattr(cfg, k) for k in keys] == [f"{k}-59" for k in keys]  # ни одно изменение не затёрто
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_processes_do_not_lose_each_others_changes(tmp_path):
    path = tmp_path / "config.toml"
    save_config(path, Config())
    code = textwrap.dedent('''
        import sys
        from pathlib import Path
        from dawg_replay.config import update_config
        for i in range(40):
            update_config(Path(sys.argv[1]), {sys.argv[2]: f"{sys.argv[2]}-{i}"})
    ''')
    procs = [subprocess.Popen([sys.executable, "-c", code, str(path), key])
             for key in ("telegram_chat_name", "telegram_bot")]
    for i in range(40):
        update_config(path, {"telegram_chat_id": f"telegram_chat_id-{i}"})
    assert [p.wait(120) for p in procs] == [0, 0]
    cfg = load_config(path)
    assert (cfg.telegram_chat_id, cfg.telegram_chat_name, cfg.telegram_bot) == (
        "telegram_chat_id-39", "telegram_chat_name-39", "telegram_bot-39")
