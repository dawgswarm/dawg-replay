import json

from dawg_replay.cli import main


def test_full_flow(cli, write_script, kr, home):
    code, data = cli("add", str(write_script(version=1)))
    assert code == 0 and data["ok"] and data["status"] == "added"
    assert data["version"]["path"].endswith("scripts/ozon-cards/v1/script.py")

    code, data = cli("add", str(write_script(version=2)))
    assert data["previous_version"] == 1

    code, data = cli("list")
    assert [s["name"] for s in data["scripts"]] == ["ozon-cards"]
    assert data["scripts"][0]["current_version"] == 2

    code, data = cli("versions", "ozon-cards")
    assert [(v["version"], v["current"]) for v in data["versions"]] == [(1, False), (2, True)]

    code, data = cli("rollback", "ozon-cards", "1")
    assert code == 0 and data["version"]["version"] == 1

    code, data = cli("preset", "save", "ozon-cards", "base", "--set", "query=ноутбук", "--set", "max_pages=3")
    assert code == 0 and data["preset"]["params"] == {"query": "ноутбук", "max_pages": 3}

    code, data = cli("preset", "list", "ozon-cards")
    assert [p["name"] for p in data["presets"]] == ["base"]

    code, data = cli("secret", "set", "ozon-cards", "sheet_token", stdin="tok-xyz\n")
    assert code == 0
    assert kr.store[("dawg-replay", "secret:ozon-cards:sheet_token")] == "tok-xyz"

    code, data = cli("show", "ozon-cards")
    assert data["manifest"]["version"] == 1
    assert data["presets"] == ["base"] and data["secrets"] == {"sheet_token": True}
    assert "tok-xyz" not in json.dumps(data)

    code, data = cli("preset", "delete", "ozon-cards", "base")
    assert code == 0

    code, data = cli("remove", "ozon-cards")
    assert code == 1 and data["error"]["code"] == "confirmation_required"
    code, data = cli("remove", "ozon-cards", "--yes")
    assert code == 0 and data["secrets_deleted"] == ["sheet_token"]
    assert cli("list")[1]["scripts"] == []


def test_preset_from_params_file(cli, write_script, tmp_path):
    cli("add", str(write_script(version=1)))
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"query": "a", "max_pages": 2}), encoding="utf-8")
    code, data = cli("preset", "save", "ozon-cards", "p", "--params", str(f), "--set", "max_pages=4")
    assert data["preset"]["params"] == {"query": "a", "max_pages": 4}


def test_errors_as_json(cli, write_script):
    code, data = cli("show", "nope")
    assert code == 1 and data == {
        "ok": False,
        "error": {"code": "script_not_found", "message": "скрипт `nope` не зарегистрирован; список: dawg-replay list"},
    }
    code, data = cli("add", str(write_script(text="print(1)\n")))
    assert code == 1 and data["error"]["code"] == "invalid_manifest"
    assert data["error"]["details"][0]["code"] == "missing_block"


def test_preset_rejects_secret(cli, write_script):
    cli("add", str(write_script(version=1)))
    code, data = cli("preset", "save", "ozon-cards", "p", "--set", "sheet_token=x")
    assert code == 1 and "секрет здесь не хранится" in data["error"]["message"]


def test_config_commands(cli, kr):
    code, data = cli("config", "show")
    assert data["api_key_set"] is False and data["config"]["max_parallel_runs"] == 3
    code, data = cli("config", "set-key", stdin="key-123\n")
    assert code == 0 and kr.store[("dawg-replay", "dawg-api-key")] == "key-123"
    code, data = cli("config", "set", "base_url", "https://x.ru")
    assert data["config"]["base_url"] == "https://x.ru"
    assert cli("config", "show")[1]["api_key_set"] is True
    code, data = cli("config", "set-key", stdin="\n")
    assert code == 1 and data["error"]["code"] == "empty_secret"


def test_human_output(home, kr, write_script, capsys):
    assert main(["add", str(write_script(version=1))]) == 0
    assert "зарегистрирован ozon-cards v1" in capsys.readouterr().out
    assert main(["show", "ozon-cards"]) == 0
    out = capsys.readouterr().out
    assert "query: string, Запрос (обязательный)" in out
    assert "sheet_token: secret, Токен (не задан)" in out
    assert main(["show", "nope"]) == 1
    assert "ошибка: скрипт `nope` не зарегистрирован" in capsys.readouterr().err
