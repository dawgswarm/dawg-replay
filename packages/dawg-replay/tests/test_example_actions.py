"""Демо examples/the-internet-actions: манифест, таблица аккаунтов, разбор сообщения сайта."""

import importlib.util
from pathlib import Path

from dawg_replay_kit.manifest import check_value, load_manifest
from dawg_replay_kit.tables import read_table

from dawg_replay.cli import main

DIR = Path(__file__).resolve().parents[3] / "examples" / "the-internet-actions"


def test_manifest_and_demo_table():
    assert main(["validate", str(DIR / "script.py")]) == 0
    m = load_manifest(DIR / "script.py")
    accounts = m.param("accounts")
    assert accounts.secret and m.contract_minor == 2
    rows = read_table(DIR / "accounts.csv")
    assert check_value(accounts, rows) is None and len(rows) == 5
    assert [r["login"] for r in rows].count("tomsmith") == 4 and rows[2]["_row"] == 4


def test_flash_text():
    spec = importlib.util.spec_from_file_location("demo_actions", DIR / "script.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.flash_text("\n  Your password is invalid!\n  ×\n") == "Your password is invalid!"
