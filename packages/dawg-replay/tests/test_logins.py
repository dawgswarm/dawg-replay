"""Сохранённые входы на стороне приложения: каталог для kit, список, «Забыть», удаление со скриптом."""

import json
import os
import stat
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.runner import ENV_LOGINS_DIR
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app
from dawg_replay_kit.logins import LoginStore

POSIX = sys.platform != "win32"
PORT = 54331
BASE = f"http://127.0.0.1:{PORT}"
STATE = {"cookies": [{"name": "sid", "value": "cookie-secret-123", "domain": ".example.com", "path": "/"}],
         "origins": [{"origin": "https://example.com", "localStorage": [{"name": "t", "value": "ls-secret-456"}]}]}

# Скрипт сохраняет вход так же, как kit.browser(login=...) при закрытии браузера.
SAVES_LOGIN = '''
import json, os
import dawg_replay_kit as kit
from dawg_replay_kit.logins import LoginStore
d = os.environ["DAWG_SCRIPT_LOGINS_DIR"]
LoginStore(d).save("buyer@example.com", %s)
(kit.output_dir / "dir.txt").write_text(d, encoding="utf-8")
kit.result("ok", "готово")
''' % json.dumps(STATE)


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def registered(app, write_script):
    f = write_script()
    f.write_text(f.read_text("utf-8").replace('print("hi")', SAVES_LOGIN), "utf-8")
    app.registry.add(f)
    return "ozon-cards"


def _save(app, name, state=STATE, script="ozon-cards"):
    return LoginStore(app.paths.login_dir(script)).save(name, state)


def test_run_gets_logins_dir_outside_run_history(registered, cli, home, kr):
    code, data = cli("run", registered, "--set", "query=q", "--follow")
    assert code == 0, data
    with App(home, keyring_backend=kr) as app:
        run_dir = app.paths.run_dir(data["run"]["id"])
        d = (run_dir / "output" / "dir.txt").read_text("utf-8")
        assert os.path.samefile(d, app.paths.login_dir(registered))
        assert [x["name"] for x in app.list_logins(registered)] == ["buyer@example.com"]
        if POSIX:
            assert stat.S_IMODE(os.stat(app.paths.logins).st_mode) == 0o700
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        # Ни события, ни логи, ни параметры запуска не содержат входа.
        for f in run_dir.rglob("*"):
            if f.is_file():
                text = f.read_text("utf-8", errors="replace")
                assert "cookie-secret" not in text and "ls-secret" not in text, f


def test_runner_strips_inherited_logins_dir(monkeypatch, tmp_path):
    from dawg_replay.runner import ScriptRun

    monkeypatch.setenv(ENV_LOGINS_DIR, "/чужой")
    s = tmp_path / "s.py"
    s.write_text("", "utf-8")
    assert ENV_LOGINS_DIR not in ScriptRun(s, "r1", tmp_path)._environment(tmp_path / "p.json")
    env = ScriptRun(s, "r1", tmp_path, logins_dir=tmp_path / "L")._environment(tmp_path / "p.json")
    assert env[ENV_LOGINS_DIR] == str((tmp_path / "L").resolve())


def test_app_list_forget(app, write_script):
    app.registry.add(write_script())
    assert app.list_logins("ozon-cards") == []
    _save(app, "b@x.ru")
    _save(app, "a@x.ru")
    items = app.list_logins("ozon-cards")
    assert [i["name"] for i in items] == ["a@x.ru", "b@x.ru"]
    assert set(items[0]) == {"name", "saved_at", "used_at", "cookies", "origins"}
    app.forget_login("ozon-cards", "a@x.ru")
    with pytest.raises(Exception, match="нет сохранённого входа"):
        app.forget_login("ozon-cards", "a@x.ru")
    assert app.forget_logins("ozon-cards") == 1
    assert app.list_logins("ozon-cards") == []
    with pytest.raises(Exception, match="не найден|нет скрипта|nope"):
        app.list_logins("nope")


def test_remove_script_deletes_logins(app, write_script):
    app.registry.add(write_script())
    app.registry.add(write_script(name="other", filename="o.py"))
    _save(app, "a@x.ru")
    _save(app, "keep", script="other")
    app.set_secret("ozon-cards", "sheet_token", "x")  # backend напрямую: удаление отсутствующего секрета падает
    r = app.remove("ozon-cards")
    assert r["logins_deleted"] == 1
    assert not app.paths.login_dir("ozon-cards").exists()
    assert [i["name"] for i in app.list_logins("other")] == ["keep"]


def test_cli_logins(cli, home, kr, write_script):
    with App(home, keyring_backend=kr) as app:
        app.registry.add(write_script())
        _save(app, "a@x.ru")
        _save(app, "b@x.ru")
    code, data = cli("logins", "list", "ozon-cards")
    assert code == 0 and [i["name"] for i in data["logins"]] == ["a@x.ru", "b@x.ru"]
    assert "cookie-secret" not in json.dumps(data)
    code, data = cli("logins", "forget", "ozon-cards")
    assert code == 1 and data["error"]["code"] == "bad_arguments"
    code, data = cli("logins", "forget", "ozon-cards", "a@x.ru")
    assert code == 0 and data["forgotten"] == 1
    code, data = cli("logins", "forget", "ozon-cards", "a@x.ru")
    assert code == 1 and data["error"]["code"] == "login_not_found"
    code, data = cli("logins", "forget", "ozon-cards", "--all")
    assert code == 0 and data["forgotten"] == 1
    code, data = cli("remove", "ozon-cards")
    assert "сохранённые входы" in data["error"]["message"]


def test_cli_logins_text(home, kr, write_script, capsys):
    from dawg_replay.cli import main

    with App(home, keyring_backend=kr) as app:
        app.registry.add(write_script())
    assert main(["logins", "list", "ozon-cards"]) == 0
    assert "сохранённых входов нет" in capsys.readouterr().out
    with App(home, keyring_backend=kr) as app:
        _save(app, "a@x.ru")
    assert main(["logins", "list", "ozon-cards"]) == 0
    out = capsys.readouterr().out
    # Местное время, как в интерфейсе («сегодня 18:43»), а не ISO в UTC.
    assert out.startswith("a@x.ru  сохранён сегодня ") and "ещё не использовался" in out and "Z" not in out


# --- веб ---


@pytest.fixture
def web(home, kr, write_script):
    with App(home, keyring_backend=kr) as a:
        a.registry.add(write_script())
    s = Secrets(port=PORT)
    with TestClient(create_app(home, s, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={s.token}").status_code == 303
        yield c, s


def _post(c, s, url, data):
    return c.post(url, data=data, headers={"Origin": BASE, "X-CSRF-Token": s.csrf})


def test_web_block_hidden_without_logins(web):
    c, _ = web
    r = c.get("/scripts/ozon-cards")
    assert r.status_code == 200 and "Сохранённые входы" not in r.text


def test_web_list_and_forget(web, home, kr):
    c, s = web
    with App(home, keyring_backend=kr) as app:
        _save(app, 'a"<b>@x.ru')
        _save(app, "b@x.ru")
    r = c.get("/scripts/ozon-cards")
    assert "Сохранённые входы" in r.text and "2 входа" in r.text
    assert "a&#34;&lt;b&gt;@x.ru" in r.text and "Забыть все" in r.text
    assert "cookie-secret" not in r.text

    r = _post(c, s, "/scripts/ozon-cards/logins/forget", {"_login": 'a"<b>@x.ru'})
    assert r.status_code == 200 and "стёрт" in r.text and " open" in r.text
    assert "Забыть все" not in r.text  # остался один вход
    r = _post(c, s, "/scripts/ozon-cards/logins/forget", {"_login": "нет-такого"})
    assert "нет сохранённого входа" in r.text
    r = _post(c, s, "/scripts/ozon-cards/logins/forget", {"_all": "1"})
    assert "Стёрто входов: 1" in r.text
    with App(home, keyring_backend=kr) as app:
        assert app.list_logins("ozon-cards") == []


def test_web_forget_needs_csrf(web, home, kr):
    c, _ = web
    with App(home, keyring_backend=kr) as app:
        _save(app, "a@x.ru")
    r = c.post("/scripts/ozon-cards/logins/forget", data={"_all": "1"}, headers={"Origin": BASE})
    assert r.status_code == 403
    with App(home, keyring_backend=kr) as app:
        assert len(app.list_logins("ozon-cards")) == 1
