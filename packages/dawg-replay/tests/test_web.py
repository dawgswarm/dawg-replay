import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fake_dawg import KEY, FakeDawg
from starlette.testclient import TestClient

from dawg_replay.app import App
from dawg_replay.web import views
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54321
BASE = f"http://127.0.0.1:{PORT}"
TESTING = Path(__file__).resolve().parents[3] / "testing"


@pytest.fixture
def fake():
    f = FakeDawg().start()
    yield f
    f.stop()


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def client(home, kr, secrets_):
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        yield c


@pytest.fixture
def logged(client, secrets_):
    r = client.get(f"/?token={secrets_.token}")
    assert r.status_code == 303
    return client


def post(c, url, data=None, csrf=None, origin=BASE, **headers):
    h = dict(headers)
    if origin is not None:
        h["Origin"] = origin
    if csrf is not None:
        h["X-CSRF-Token"] = csrf
    return c.post(url, data=data or {}, headers=h)


# --- токен, Host, метод ---


def test_no_token_is_forbidden(client):
    for path in ("/", "/scripts", "/settings", "/static/app.css", "/events/runs"):
        assert client.get(path).status_code == 403, path


def test_login_sets_strict_httponly_cookie_and_strips_token(client, secrets_):
    r = client.get(f"/scripts?token={secrets_.token}&x=1")
    assert r.status_code == 303
    assert r.headers["location"] == "/scripts?x=1"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"dawg_replay_{PORT}={secrets_.token};")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert client.get("/scripts").status_code == 200


def test_wrong_token(client):
    assert client.get("/?token=nope").status_code == 403
    assert client.get("/scripts").status_code == 403


def test_wrong_cookie(client):
    client.cookies.set(f"dawg_replay_{PORT}", "nope")
    assert client.get("/scripts").status_code == 403


@pytest.mark.parametrize(
    "host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1", "127.0.0.1:1", f"0.0.0.0:{PORT}", f"[::1]:{PORT}"]
)
def test_foreign_host_forbidden_even_with_cookie(logged, host, secrets_):
    assert logged.get("/scripts", headers={"Host": host}).status_code == 403
    # и вход по токену с чужим Host не ставит cookie
    r = logged.get(f"/?token={secrets_.token}", headers={"Host": host})
    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_localhost_host_allowed(logged):
    assert logged.get("/scripts", headers={"Host": f"localhost:{PORT}"}).status_code == 200


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_methods_rejected(logged, method):
    assert logged.request(method, "/settings/limits").status_code == 405


def test_security_headers(logged):
    r = logged.get("/scripts")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


# --- CSRF и Origin ---

LIMITS = {"max_parallel_runs": "5", "max_timeout_minutes": "120"}


def test_post_without_csrf_forbidden(logged, home):
    assert post(logged, "/settings/limits", LIMITS).status_code == 403
    assert App(home).config.max_parallel_runs == 3


def test_post_wrong_csrf_forbidden(logged):
    assert post(logged, "/settings/limits", LIMITS, csrf="nope").status_code == 403


@pytest.mark.parametrize("origin", ["http://evil.example", f"http://evil.example:{PORT}", "null", f"https://127.0.0.1:{PORT}"])
def test_post_foreign_origin_forbidden(logged, secrets_, origin):
    assert post(logged, "/settings/limits", LIMITS, csrf=secrets_.csrf, origin=origin).status_code == 403


def test_post_without_origin_and_referer_forbidden(logged, secrets_):
    assert post(logged, "/settings/limits", LIMITS, csrf=secrets_.csrf, origin=None).status_code == 403


def test_post_referer_fallback(logged, secrets_, home):
    r = post(logged, "/settings/limits", LIMITS, csrf=secrets_.csrf, origin=None, Referer=f"{BASE}/settings")
    assert r.status_code == 200
    assert App(home).config.max_parallel_runs == 5


def test_post_without_cookie_forbidden(client, secrets_):
    assert post(client, "/settings/limits", LIMITS, csrf=secrets_.csrf).status_code == 403


def test_csrf_token_is_in_page(logged, secrets_):
    html = logged.get("/scripts").text
    assert f'<meta name="csrf-token" content="{secrets_.csrf}">' in html
    assert secrets_.token not in html


# --- экран Скрипты ---


def test_scripts_empty(logged):
    html = logged.get("/scripts").text
    assert "Скриптов пока нет" in html and "dawg-replay add script.py" in html


def test_scripts_list_with_last_run(logged, home, write_script):
    with App(home) as app:
        app.registry.add(write_script(version=2, title="Ozon: карточки"))
        app.registry.add(write_script(version=1, name="never-run", title="Без запусков"))
        app.runs.reserve(
            run_id="r1", script_name="ozon-cards", script_version=2, script_path="/x", draft=False,
            preset=None, params={}, timeout_seconds=60, limit=10,
        )
        app.runs.finish("r1", status="ok", reason=None, summary="собрано 40 карточек")
    html = logged.get("/scripts").text
    assert "Ozon: карточки" in html and "ozon-cards, версия 2" in html
    assert 'class="status status-ok">готово' in html and "собрано 40 карточек" in html
    assert 'href="/runs/r1"' in html and 'href="/scripts/ozon-cards"' in html
    assert "Ещё не запускался" in html
    assert logged.get("/scripts/_list").status_code == 200
    assert logged.get("/scripts/ozon-cards").status_code == 200
    assert logged.get("/scripts/nope").status_code == 404
    assert logged.get("/runs/r1").status_code == 200


def test_status_words():
    assert views.status_view("failed (timeout)") == {"text": "превышено время", "kind": "failed"}
    assert views.status_view("running")["kind"] == "running"
    assert views.number(179953.4) == "179 953"


def test_sse_stream_sends_changes_and_stops():
    """Генератор SSE напрямую: TestClient на части версий ждёт конца бесконечного ответа."""
    import asyncio
    from types import SimpleNamespace

    from dawg_replay.web.server import sse_stream

    values = iter(["a", "a", "b"])
    state = SimpleNamespace(stopping=asyncio.Event())
    polls = []

    class Req:
        app = SimpleNamespace(state=SimpleNamespace(ui=state))

        async def is_disconnected(self):
            return len(polls) >= 3

    def poll():
        v = next(values)
        polls.append(v)
        return v

    async def collect():
        return [chunk async for chunk in sse_stream(Req(), poll, interval=0.01)]

    out = asyncio.run(collect())
    assert out == ["retry: 3000\n\n", "event: change\ndata: a\n\n", "event: change\ndata: b\n\n"]

    async def stopped():
        state.stopping.set()
        return [c async for c in sse_stream(Req(), lambda: "x", interval=0.01)]

    assert asyncio.run(stopped()) == ["retry: 3000\n\n"]


def test_sse_route_requires_cookie(client):
    assert client.get("/events/runs").status_code == 403


# --- экран Настройки ---


@pytest.fixture
def with_fake_base(home, fake):
    with App(home) as app:
        app.set_config("base_url", fake.url)


def test_settings_without_key(logged):
    html = logged.get("/settings").text
    assert "Ключ не задан" in html
    assert "Сохраните ключ" in logged.get("/settings/usage").text


def test_settings_key_saved_after_check(logged, secrets_, kr, fake, with_fake_base):
    r = post(logged, "/settings/key", {"api_key": KEY}, csrf=secrets_.csrf)
    assert r.status_code == 200 and r.headers["hx-trigger"] == "key-saved"
    assert "Ключ проверен и сохранён" in r.text and "тариф «test»" in r.text
    assert KEY not in r.text and "••••" + KEY[-4:] in r.text
    assert kr.store[("dawg-replay", "dawg-api-key")] == KEY
    html = logged.get("/settings").text
    assert KEY not in html
    usage = logged.get("/settings/usage").text
    assert "587" in usage and "из 600" in usage and "0 из 5" in usage
    r = post(logged, "/settings/key/check", csrf=secrets_.csrf)
    assert "Ключ работает" in r.text


def test_settings_bad_key_not_saved(logged, secrets_, kr, fake, with_fake_base):
    r = post(logged, "/settings/key", {"api_key": "wrong-key"}, csrf=secrets_.csrf)
    assert "ключ DAWG не подходит" in r.text and "hx-trigger" not in r.headers
    assert ("dawg-replay", "dawg-api-key") not in kr.store
    r = post(logged, "/settings/key", {"api_key": "  "}, csrf=secrets_.csrf)
    assert "ключ DAWG пустой" in r.text


def test_settings_key_platform_down(logged, secrets_, kr, home):
    with App(home) as app:
        app.set_config("base_url", "http://127.0.0.1:9")
    r = post(logged, "/settings/key", {"api_key": KEY}, csrf=secrets_.csrf)
    assert "платформа DAWG недоступна" in r.text
    assert ("dawg-replay", "dawg-api-key") not in kr.store


def test_settings_limits(logged, secrets_, home):
    r = post(logged, "/settings/limits", {"max_parallel_runs": "7", "max_timeout_minutes": "90"}, csrf=secrets_.csrf)
    assert "Ограничения сохранены" in r.text
    cfg = App(home).config
    assert (cfg.max_parallel_runs, cfg.max_timeout_minutes) == (7, 90)
    r = post(logged, "/settings/limits", {"max_parallel_runs": "0", "max_timeout_minutes": "30"}, csrf=secrets_.csrf)
    assert "не меньше 1" in r.text
    r = post(logged, "/settings/limits", {"max_parallel_runs": "2", "max_timeout_minutes": "abc"}, csrf=secrets_.csrf)
    assert "должен быть целым" in r.text
    cfg = App(home).config
    assert (cfg.max_parallel_runs, cfg.max_timeout_minutes) == (7, 90)  # ничего не сохранено частично


# --- запуск из UI и остановка сервера ---

MANIFEST = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

# /// dawg-script
# contract = 1
# name = "ui-run"
# title = "Запуск из UI"
# version = 1
#
# [[params]]
# name = "seconds"
# type = "int"
# title = "Сколько работать"
# default = 60
# ///
'''

SCRIPT = '''
import time
import dawg_replay_kit as kit
from dawg_replay_kit.browser import Connection

k = kit.init()
k.browsers.connect = lambda url: Connection("B", "C", "P", lambda: None)
with kit.browser() as b:
    kit.log.info("работаю")
    for i in range(kit.params.seconds):
        time.sleep(1)
kit.result("ok", "готово")
'''


def test_run_from_ui_starts_and_finishes(logged, secrets_, home, write_script, monkeypatch, tmp_path):
    from dawg_replay import launch

    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    f = tmp_path / "ui-run.py"
    f.write_text(MANIFEST + textwrap.dedent(SCRIPT.replace("with kit.browser() as b:", "if True:")), encoding="utf-8")
    with App(home) as app:
        app.registry.add(f)
    r = post(logged, "/scripts/ui-run/run", {"seconds": "0"}, csrf=secrets_.csrf)
    assert r.status_code == 204
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    end = time.monotonic() + 20
    while time.monotonic() < end:
        with App(home) as app:
            rec = app.runs.get(run_id)
        if not rec.running:
            break
        time.sleep(0.1)
    assert rec.label == "ok" and rec.params["seconds"] == 0
    with App(home) as app:
        assert app.runs.get(run_id).supervisor_pid == os.getpid()  # супервизор: процесс сервера


def test_corrupt_key_in_settings_and_run(logged, secrets_, home, kr, write_script, tmp_path):
    kr.set_password("dawg-replay", "dawg-api-key", "\x1b[2~SECRET")
    html = logged.get("/settings").text
    assert "ключ повреждён" in html and "Проверить сохранённый ключ" not in html and "SECRET" not in html
    r = post(logged, "/settings/key/check", csrf=secrets_.csrf)
    assert "ключ DAWG повреждён, задайте заново" in r.text
    f = tmp_path / "ui-run.py"
    f.write_text(MANIFEST + textwrap.dedent(SCRIPT), encoding="utf-8")
    with App(home) as app:
        app.registry.add(f)
    r = post(logged, "/scripts/ui-run/run", {"seconds": "0"}, csrf=secrets_.csrf)
    assert r.status_code == 200 and "ключ DAWG повреждён, задайте заново: dawg-replay config set-key" in r.text
    with App(home) as app:
        assert app.runs.list() == []


def test_settings_key_form_strips_paste_markers(logged, secrets_, kr, fake, with_fake_base):
    r = post(logged, "/settings/key", {"api_key": "\x1b[200~" + KEY + "\x1b[201~"}, csrf=secrets_.csrf)
    assert "Ключ проверен и сохранён" in r.text
    assert kr.store[("dawg-replay", "dawg-api-key")] == KEY
    r = post(logged, "/settings/key", {"api_key": "a\x01SECRET"}, csrf=secrets_.csrf)
    assert "невидимые управляющие символы" in r.text and "pbpaste" not in r.text and "SECRET" not in r.text
    assert kr.store[("dawg-replay", "dawg-api-key")] == KEY


def test_run_from_ui_error_shown(logged, secrets_):
    r = post(logged, "/scripts/nope/run", {}, csrf=secrets_.csrf)
    assert r.status_code == 200 and "не зарегистрирован" in r.text


def _server_env(home, fake):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DAWG_SCRIPT_", "DAWG_SECRET_"))}
    env.update(
        DAWG_REPLAY_HOME=str(home),
        DAWG_API_KEY=KEY,
        PYTHON_KEYRING_BACKEND="envkeyring.EnvKeyring",
        PYTHONPATH=os.pathsep.join([str(TESTING), env.get("PYTHONPATH", "")]),
        DAWG_REPLAY_SCRIPT_COMMAND=json.dumps([sys.executable]),
    )
    return env


def _start_server(env):
    p = subprocess.Popen(
        [sys.executable, "-m", "dawg_replay", "--json", "ui", "--no-open"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    info = json.loads(p.stdout.readline())
    return p, info


def _login(info):
    import httpx

    c = httpx.Client(base_url=f"http://127.0.0.1:{info['port']}", follow_redirects=False, trust_env=False)
    assert c.get(info["url"].split(str(info["port"]), 1)[1]).status_code == 303
    html = c.get("/scripts").text
    csrf = html.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
    return c, csrf


def _ui_run(c, csrf, port):
    r = c.post(
        "/scripts/ui-run/run",
        data={"seconds": "120"},
        headers={"X-CSRF-Token": csrf, "Origin": f"http://127.0.0.1:{port}"},
    )
    assert r.status_code == 204, r.text
    return r.headers["hx-redirect"].rsplit("/", 1)[1]


def _wait_working(home, run_id, timeout=30):
    path = home / "runs" / run_id / "events.jsonl"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and "работаю" in path.read_text(encoding="utf-8"):
            return True
        time.sleep(0.1)
    return False


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def registered_ui_script(home, tmp_path, kr, fake):
    f = tmp_path / "ui-run.py"
    f.write_text(MANIFEST + textwrap.dedent(SCRIPT), encoding="utf-8")
    with App(home) as app:
        app.registry.add(f)
        app.set_config("base_url", fake.url)
        app.set_config("cancel_grace_seconds", "10")
    return f


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM серверу")
def test_server_stop_cancels_its_runs(home, fake, registered_ui_script):
    server, info = _start_server(_server_env(home, fake))
    try:
        c, csrf = _login(info)
        ids = [_ui_run(c, csrf, info["port"]) for _ in range(2)]
        for rid in ids:
            assert _wait_working(home, rid)
        assert len(fake.sessions) == 2
        with App(home) as app:
            pids = [app.runs.get(rid).pid for rid in ids]
        t0 = time.monotonic()
        server.send_signal(signal.SIGTERM)
        # uvicorn после штатной остановки заново поднимает пойманный сигнал: код -SIGTERM.
        assert server.wait(60) in (0, -signal.SIGTERM)
        took = time.monotonic() - t0
    finally:
        if server.poll() is None:
            server.kill()
    with App(home) as app:
        recs = [app.runs.get(rid) for rid in ids]
    assert [r.label for r in recs] == ["failed (cancelled)"] * 2
    assert not any(r.killed for r in recs)  # ушли по мягкому сигналу
    assert took < 15
    assert fake.sessions == {}  # kit закрыл браузеры сам
    assert not any(_alive(p) for p in pids)
    for rid in ids:
        events = (home / "runs" / rid / "events.jsonl").read_text(encoding="utf-8")
        assert '"action":"close"' in events
    assert "останавливаю запуски (2)" in server.stderr.read()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")
def test_server_killed_runs_recovered_on_next_start(home, fake, registered_ui_script):
    env = _server_env(home, fake)
    server, info = _start_server(env)
    c, csrf = _login(info)
    rid = _ui_run(c, csrf, info["port"])
    assert _wait_working(home, rid)
    with App(home) as app:
        script_pid = app.runs.get(rid).pid
    server.kill()
    server.wait(10)
    assert _alive(script_pid) and len(fake.sessions) == 1  # сирота, пока приложение не проснулось

    # Следующий старт сервера закрывает потерянный запуск: дерево убито, браузер освобождён страховкой.
    # heartbeat свежий, но супервизора (убитого сервера) нет: ждём признания потерянным.
    with App(home) as app:
        app.db.execute("UPDATE runs SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?", (rid,))
    server2, info2 = _start_server(env)
    try:
        end = time.monotonic() + 20
        while time.monotonic() < end:
            with App(home) as app:
                if not app.runs.get(rid).running:
                    break
            time.sleep(0.2)
    finally:
        server2.send_signal(signal.SIGTERM)
        server2.wait(30)
    with App(home) as app:
        rec = app.runs.get(rid)
    assert rec.label == "failed (lost)" and rec.killed
    assert not _alive(script_pid)
    assert fake.sessions == {}


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT серверу")
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_server_stop_with_open_sse_is_clean(home, fake, sig):
    """Остановка сервера с открытой страницей (SSE): потоки закрываются сами, stderr без traceback."""
    import threading

    server, info = _start_server(_server_env(home, fake))
    closed = threading.Event()
    try:
        c, _ = _login(info)

        def listen():
            try:
                with c.stream("GET", "/events/runs", timeout=None) as r:
                    for _ in r.iter_lines():
                        pass
            except Exception:
                pass
            closed.set()

        threading.Thread(target=listen, daemon=True).start()
        time.sleep(1.0)  # поток SSE открыт и получил первое значение
        t0 = time.monotonic()
        server.send_signal(sig)
        # Ctrl+C: CLI выходит с кодом 130; SIGTERM: uvicorn заново поднимает сигнал.
        assert server.wait(30) in (0, 130, -sig)
        took = time.monotonic() - t0
    finally:
        if server.poll() is None:
            server.kill()
    err = server.stderr.read()
    assert "Traceback" not in err and "ERROR" not in err, err
    assert closed.wait(5)
    assert took < 2  # SSE не держит остановку до timeout_graceful_shutdown
