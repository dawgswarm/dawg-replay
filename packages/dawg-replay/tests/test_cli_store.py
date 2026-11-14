import json

from dawg_replay.app import App
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


PASTED = "\x1b[200~key-PASTED-77\x1b[201~"


def test_set_key_strips_bracketed_paste(cli, kr):
    code, data = cli("config", "set-key", stdin=f"  {PASTED} \n")
    assert code == 0 and kr.store[("dawg-replay", "dawg-api-key")] == "key-PASTED-77"


def test_set_key_rejects_control_chars(cli, kr):
    code, data = cli("config", "set-key", stdin="key\x01SECRET\n")
    assert code == 2 and data["error"]["code"] == "bad_secret"
    assert "управляющие символы" in data["error"]["message"] and "pbpaste |" in data["error"]["message"]
    assert "SECRET" not in json.dumps(data)
    assert ("dawg-replay", "dawg-api-key") not in kr.store
    code, data = cli("config", "set-key", stdin="\x1b[200~\x1b[201~\n")
    assert code == 1 and data["error"]["code"] == "empty_secret"


def test_secret_set_cleans_and_rejects(cli, write_script, kr):
    cli("add", str(write_script(version=1)))
    code, _ = cli("secret", "set", "ozon-cards", "sheet_token", stdin=PASTED + "\n")
    assert code == 0 and kr.store[("dawg-replay", "secret:ozon-cards:sheet_token")] == "key-PASTED-77"
    code, data = cli("secret", "set", "ozon-cards", "sheet_token", stdin="tok\x7fSECRET\n")
    assert code == 2 and "dawg-replay secret set ozon-cards sheet_token" in data["error"]["message"]
    assert "SECRET" not in json.dumps(data)
    assert kr.store[("dawg-replay", "secret:ozon-cards:sheet_token")] == "key-PASTED-77"


def test_config_show_corrupt_key(home, kr, capsys):
    kr.set_password("dawg-replay", "dawg-api-key", "\x1b[2~SECRET")
    assert main(["--json", "config", "show"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["api_key_set"] is True and data["api_key_corrupt"] is True
    assert main(["config", "show"]) == 0
    out = capsys.readouterr().out
    assert "ключ DAWG: задан, но повреждён" in out and "SECRET" not in out


def test_human_bad_secret(home, kr, capsys, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("a\x01SECRET\n"))
    assert main(["config", "set-key"]) == 2
    captured = capsys.readouterr()
    assert "ошибка: ключ DAWG содержит невидимые" in captured.err
    assert "SECRET" not in captured.err + captured.out


def test_human_output(home, kr, write_script, capsys):
    assert main(["add", str(write_script(version=1))]) == 0
    assert "зарегистрирован ozon-cards v1" in capsys.readouterr().out
    assert main(["show", "ozon-cards"]) == 0
    out = capsys.readouterr().out
    assert "query: string, Запрос (обязательный)" in out
    assert "sheet_token: secret, Токен (не задан)" in out
    assert main(["show", "nope"]) == 1
    assert "ошибка: скрипт `nope` не зарегистрирован" in capsys.readouterr().err


def test_site_and_tags_in_list_and_show(cli, write_script, home, capsys):
    cli("add", str(write_script(version=1)))  # контракт 1, без site и tags
    v2 = write_script(version=2).read_text(encoding="utf-8")
    v2 = v2.replace("# contract = 1\n", '# contract = "1.1"\n').replace(
        "# version = 2\n", '# site = "Ozon"\n# tags = ["Цены", "Карточки"]\n# version = 2\n'
    )
    code, data = cli("add", str(write_script(text=v2)))
    assert code == 0 and data["script"]["site"] == "Ozon" and data["version"]["tags"] == ["Цены", "Карточки"]

    script = cli("list")[1]["scripts"][0]
    assert script["site"] == "Ozon" and script["tags"] == ["Цены", "Карточки"]
    versions = cli("versions", "ozon-cards")[1]["versions"]
    assert [(v["site"], v["tags"]) for v in versions] == [(None, []), ("Ozon", ["Цены", "Карточки"])]
    shown = cli("show", "ozon-cards")[1]
    assert shown["manifest"]["site"] == "Ozon" and shown["script"]["tags"] == ["Цены", "Карточки"]

    assert main(["show", "ozon-cards"]) == 0
    out = capsys.readouterr().out
    assert "сайт: Ozon" in out and "метки: Цены, Карточки" in out
    assert main(["list"]) == 0
    assert "(Ozon)" in capsys.readouterr().out

    cli("rollback", "ozon-cards", "1")
    script = cli("list")[1]["scripts"][0]
    assert script["site"] is None and script["tags"] == []


def test_new_fields_need_contract_11(cli, write_script):
    text = write_script(version=1).read_text(encoding="utf-8").replace("# version = 1\n", '# site = "Ozon"\n# version = 1\n')
    code, data = cli("add", str(write_script(text=text)))
    assert code == 1 and data["error"]["details"][0]["code"] == "field_needs_minor"


def test_remove_refused_while_running(cli, write_script, home):
    cli("add", str(write_script(version=1)))
    with App(home) as app:
        app.runs.reserve(
            run_id="20261114-000000-bbbbbb", script_name="ozon-cards", script_version=1, script_path="/x/script.py",
            draft=False, preset=None, params={}, timeout_seconds=60, limit=10,
        )
    code, data = cli("remove", "ozon-cards", "--yes")
    assert code == 1 and data["error"]["code"] == "script_running"
    assert data["error"]["details"] == {"runs": ["20261114-000000-bbbbbb"]}
    assert [s["name"] for s in cli("list")[1]["scripts"]] == ["ozon-cards"]
