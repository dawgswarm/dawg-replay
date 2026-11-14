"""Аудит v1 (docs/audit/2026-11-15-v1.md): защита всех маршрутов и экранирование данных скрипта.

Маршруты перебираются из приложения, поэтому новый маршрут попадает под проверку сам.
"""

import json
import re

import pytest
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from dawg_replay.app import App
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54331
BASE = f"http://127.0.0.1:{PORT}"
RUN_ID = "20261115-010000-aaaaaa"
SAMPLE = {"name": "ozon-cards", "run_id": RUN_ID, "import_id": "A" * 22, "path": "cards.csv"}


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def server(home, kr, secrets_, write_script):
    with App(home) as app:
        app.registry.add(write_script(version=1))
    return create_app(home, secrets_, keyring_backend=kr)


def routes(app):
    out = []
    for r in app.routes:
        if isinstance(r, Mount):
            out.append(("GET", r.path + "/app.js"))
        elif isinstance(r, Route):
            path = re.sub(r"\{(\w+)(:\w+)?\}", lambda m: SAMPLE.get(m.group(1), "x"), r.path)
            for method in sorted(r.methods or {"GET"}):
                if method != "HEAD":
                    out.append((method, path))
    return out


def test_every_route_needs_token_and_own_host(server, secrets_):
    found = routes(server)
    assert ("POST", "/scripts/ozon-cards/delete") in found and ("GET", "/events/runs") in found
    with TestClient(server, base_url=BASE, follow_redirects=False) as c:
        for method, path in found:
            h = {"Origin": BASE, "X-CSRF-Token": secrets_.csrf}
            assert c.request(method, path, headers=h).status_code == 403, (method, path)
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        for method, path in found:
            h = {"Origin": BASE, "X-CSRF-Token": secrets_.csrf, "Host": "evil.example"}
            assert c.request(method, path, headers=h).status_code == 403, (method, path, "host")


GOOD_CSRF = object()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": BASE},
        {"Origin": BASE, "X-CSRF-Token": "nope"},
        {"Origin": "http://evil.example", "X-CSRF-Token": GOOD_CSRF},
        {"Origin": f"http://127.0.0.1:{PORT}.evil.example", "X-CSRF-Token": GOOD_CSRF},
        {"Origin": "null", "X-CSRF-Token": GOOD_CSRF},
        {"Referer": f"http://127.0.0.1:{PORT}@evil.example/", "X-CSRF-Token": GOOD_CSRF},
        {"X-CSRF-Token": GOOD_CSRF},
    ],
    ids=["no-csrf", "bad-csrf", "foreign-origin", "suffix-origin", "null-origin", "userinfo-referer", "no-origin"],
)
def test_every_post_needs_csrf_and_own_origin(server, secrets_, home, headers):
    h = {k: (secrets_.csrf if v is GOOD_CSRF else v) for k, v in headers.items()}
    with TestClient(server, base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        for method, path in routes(server):
            if method == "POST":
                r = c.post(path, headers=h, data={"confirm": "ozon-cards", "session_id": "s1"})
                assert r.status_code == 403, (path, headers)
    with App(home) as app:
        assert [s.name for s in app.registry.list()] == ["ozon-cards"]  # удаление не прошло


XSS = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


def test_script_data_is_escaped_everywhere(home, kr, secrets_, write_script):
    """Строки из манифеста, событий и stderr приходят с сайта или от скрипта: в HTML только экранированными."""
    path = write_script(version=1)
    text = path.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^# title = .*$', f"# title = {json.dumps('Карточки ' + XSS, ensure_ascii=False)}", text)
    text = text.replace("# version = 1", "# version = 1\n# description = " + json.dumps("Описание " + XSS, ensure_ascii=False), 1)
    path.write_text(text, encoding="utf-8")
    with App(home) as app:
        app.registry.add(path)
        app.runs.reserve(run_id=RUN_ID, script_name="ozon-cards", script_version=1, script_path=str(path),
                         draft=False, preset=None, params={"query": XSS}, timeout_seconds=60, limit=100)
        run_dir = app.paths.run_dir(RUN_ID)
        (run_dir / "output").mkdir(parents=True)
        (run_dir / "output" / "a.csv").write_text("x", encoding="utf-8")
        ts = "2026-11-15T01:00:00.000Z"
        events = [
            {"t": "stage", "ts": ts, "name": XSS},
            {"t": "progress", "ts": ts, "id": "p", "label": XSS, "done": 1, "total": 2},
            {"t": "metric", "ts": ts, "key": "m", "label": XSS, "value": XSS, "unit": XSS},
            {"t": "rows", "ts": ts, "table": XSS, "columns": [XSS], "rows": [{XSS: XSS}]},
            {"t": "log", "ts": ts, "level": "warn", "msg": XSS + " [ссылка](javascript:alert(3)) **" + XSS + "**"},
            {"t": "artifact", "ts": ts, "path": "a.csv", "label": XSS},
            {"t": "session", "ts": ts, "action": "open", "session_id": XSS, "browser_id": XSS},
            {"t": XSS, "ts": ts},
            {"t": "result", "ts": ts, "status": "partial", "summary": XSS},
        ]
        (run_dir / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
        (run_dir / "stderr.log").write_text(XSS + "\n", encoding="utf-8")
        app.runs.finish(RUN_ID, status="failed", reason="exit_code", exit_code=1, summary=XSS)

    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        pages = ["/", "/home/_live", "/scripts", "/scripts/_list", "/scripts/ozon-cards", "/scripts/ozon-cards/_runs",
                 "/scripts/ozon-cards/_form", f"/runs/{RUN_ID}"]
        for url in pages:
            r = c.get(url)
            assert r.status_code == 200, url
            assert "<script>alert" not in r.text and "<img src=x" not in r.text, url
            assert "javascript:alert" not in r.text.replace("javascript:alert(3))", ""), url
        esc = "&lt;script&gt;alert(1)&lt;/script&gt;"
        assert "Карточки " + esc in c.get("/scripts").text  # title из манифеста дошёл до страницы
        assert "Описание " + esc in c.get("/scripts/ozon-cards").text
        run_html = c.get(f"/runs/{RUN_ID}").text
        assert run_html.count(esc) >= 8  # этап, прогресс, метрика, таблица, журнал, файл, итог, stderr
        with c.stream("GET", f"/runs/{RUN_ID}/events") as r:
            body = "".join(r.iter_text())
        for line in body.splitlines():
            if line.startswith("data: "):
                data = json.dumps(json.loads(line[6:]), ensure_ascii=False)
                assert "<script>alert" not in data and "<img src=x" not in data
        r = c.get(f"/runs/{RUN_ID}/stderr.log")
        assert r.headers["content-type"].startswith("text/plain") and "attachment" in r.headers["content-disposition"]
        assert r.headers["x-content-type-options"] == "nosniff"
