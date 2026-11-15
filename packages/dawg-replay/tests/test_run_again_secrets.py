"""dr-90: «Запустить снова» у запуска с секретным файлом и секретной таблицей."""

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

SCRIPT = '''# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "certs"
# title = "Сертификаты"
# version = 1
#
# [[params]]
# name = "query"
# type = "string"
# title = "Запрос"
# default = "q"
#
# [[params]]
# name = "cert"
# type = "file"
# title = "Сертификат"
# secret = true
# required = true
#
# [[params]]
# name = "logins"
# type = "table"
# title = "Входы"
# secret = true
# ///

import json
import dawg_replay_kit as kit

p = kit.params
cert = open(p.cert["path"], encoding="utf-8").read()
(kit.output_dir / "got.json").write_text(json.dumps({"cert": cert, "logins": p.logins}), encoding="utf-8")
kit.result("ok", "готово")
'''
PORT = 54331
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def certs(app, tmp_path):
    f = tmp_path / "certs.py"
    f.write_text(SCRIPT, encoding="utf-8")
    app.registry.add(f)
    cert = tmp_path / "cert.pem"
    cert.write_text("-----BEGIN SECRET-----", encoding="utf-8")
    logins = tmp_path / "logins.csv"
    logins.write_text("login,pass\nivan,Pa$$w0rd\n", encoding="utf-8")
    return app, cert, logins


def got(home, run_id):
    return json.loads((Path(home) / "runs" / run_id / "output" / "got.json").read_text(encoding="utf-8"))


def test_again_uses_live_copies_then_asks_to_upload(certs, cli, home):
    app, cert, logins = certs
    code, data = cli("run", "certs", "--param", f"cert=@{cert}", "--param", f"logins=@{logins}", "--follow")
    assert code == 0, data
    first = data["run"]
    assert first["params"]["cert"] == {"name": "cert.pem"} or first["params"]["cert"] == [{"name": "cert.pem"}]

    with App(home) as a:
        values = launch.again_values(a, a.runs.get(first["id"]))
    assert Path(values["cert"]["path"]).read_text(encoding="utf-8") == "-----BEGIN SECRET-----"
    assert values["logins"][0]["login"] == "ivan" and values["query"] == "q"

    # копия для повтора устарела: повтор не начинается и просит загрузить заново
    for f in (Path(home) / "uploads").glob("*.bin"):
        f.unlink()
    with App(home) as a, pytest.raises(Exception) as e:
        launch.again_values(a, a.runs.get(first["id"]))
    assert e.value.code == "needs_upload" and "секретного файла «Сертификат»" in e.value.message


@pytest.fixture
def web(home, kr, certs):
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": secrets_.csrf}


def test_web_run_again(certs, cli, home, web):
    app, cert, logins = certs
    _, data = cli("run", "certs", "--param", f"cert=@{cert}", "--param", f"logins=@{logins}", "--follow")
    run_id = data["run"]["id"]
    c, h = web
    r = c.post(f"/runs/{run_id}/again", headers=h)
    assert r.status_code == 204, r.text
    again = r.headers["hx-redirect"].rsplit("/", 1)[1]
    import time

    for _ in range(400):
        with App(home) as a:
            if not a.runs.get(again).running:
                break
        time.sleep(0.05)
    assert got(home, again) == {"cert": "-----BEGIN SECRET-----", "logins": [{"login": "ivan", "pass": "Pa$$w0rd", "_row": 2}]}

    for f in (Path(home) / "uploads").glob("*.bin"):
        f.unlink()
    r = c.post(f"/runs/{run_id}/again", headers=h)
    assert r.status_code == 200 and "Запуск не начат" in r.text and "загрузите заново" in r.text.lower()
    assert "Открыть форму запуска" in r.text
