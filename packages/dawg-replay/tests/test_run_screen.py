"""Экран «Запуск» (dawg_replay.web.run_screen)."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from dawg_replay import insurance
from dawg_replay.app import App
from dawg_replay.runview import RunView
from dawg_replay.web import run_screen
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54322
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def secrets_():
    return Secrets(port=PORT)


@pytest.fixture
def client(home, kr, secrets_):
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c


def post(c, secrets_, url, data=None):
    return c.post(url, data=data or {}, headers={"Origin": BASE, "X-CSRF-Token": secrets_.csrf})


def ev(t, **f):
    return {"t": t, "ts": "2026-11-14T12:00:00.000Z", **f}


@pytest.fixture
def make_run(home):
    def make(events=(), *, finish=None, raw_lines=(), run_id="20261114-120000-aaaaaa", draft=True):
        with App(home) as app:
            app.runs.reserve(run_id=run_id, script_name="ozon-search-cards", script_version=None, script_path="/x/script.py",
                             draft=draft, preset=None, params={}, timeout_seconds=60, limit=100)
            run_dir = app.paths.run_dir(run_id)
            (run_dir / "output").mkdir(parents=True, exist_ok=True)
            with open(run_dir / "events.jsonl", "a", encoding="utf-8", newline="") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                for line in raw_lines:
                    f.write(line + "\n")
            if finish:
                app.runs.finish(run_id, **finish)
        return run_id, run_dir

    return make


ALL_EVENTS = [
    ev("stage", name="Открываем выдачу"),
    ev("session", action="provisioning"),
    ev("session", action="open", session_id="s1"),
    ev("stage", name="Собираем карточки"),
    ev("progress", id="pages", label="Страницы выдачи", done=2, total=3),
    ev("progress", id="seen", label="Просмотрено", done=1234),
    ev("metric", key="cards", label="Собрано карточек", value=72),
    ev("metric", key="min", label="Самая низкая цена", value=267, unit="₽"),
    ev("rows", table="cards", columns=["sku", "name"], rows=[{"sku": 1, "name": "Чайник <b>жирный</b>"}]),
    ev("log", level="info", msg="страница 1 <script>alert(1)</script>"),
    ev("log", level="warn", msg="медленно"),
    ev("log", level="error", msg="упало"),
    ev("log", level="debug", msg="отладка"),
    ev("log", level="info", msg="страховка: освобождён браузер s9", source="dawg-replay"),
    ev("artifact", path="cards.csv", label="Карточки (CSV)", mime="text/csv"),
    ev("chart", data=[1]),
]


def test_page_renders_all_event_types(client, make_run):
    run_id, run_dir = make_run(ALL_EVENTS, raw_lines=["не json"])
    (run_dir / "output" / "cards.csv").write_text("sku\n1\n", encoding="utf-8", newline="")
    r = client.get(f"/runs/{run_id}")
    assert r.status_code == 200
    h = r.text
    assert 'data-live' in h and f'data-after="' in h
    assert "Открываем выдачу" in h and "Собираем карточки" in h and 'aria-current="step"' in h
    assert "2 из 3" in h and '<progress class="meter" value="2" max="3"' in h
    assert "1\u202f234" in h  # счётчик без total
    assert "Собрано карточек" in h and "267" in h and "₽" in h
    assert "<th scope=\"col\">sku</th>" in h and "Чайник &lt;b&gt;жирный&lt;/b&gt;" in h
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h and "<script>alert" not in h
    for cls in ("lvl-info", "lvl-warn", "lvl-error", "lvl-debug", "lvl-raw", "log-app"):
        assert cls in h, cls
    assert "неизвестный тип события `chart`" in h and "битая строка" in h
    assert f'href="/runs/{run_id}/files/cards.csv"' in h and "Карточки (CSV)" in h
    assert "открыто браузеров: 1" in h
    assert "Остановить" in h and f'hx-post="/runs/{run_id}/cancel"' in h
    assert "<style" not in h.split("</head>")[1] and ' style="' not in h  # CSP: без инлайн-стилей


def test_finished_result_and_reasons(client, make_run, home):
    summary = "Собрано **72** карточки, [выдача](https://ozon.ru/search). <img src=x onerror=alert(1)> [плохая](javascript:alert(1))"
    run_id, _ = make_run([ev("result", status="partial", summary=summary)], finish={"status": "partial", "reason": None, "exit_code": 0})
    h = client.get(f"/runs/{run_id}").text
    assert "Собрано не всё" in h and "<strong>72</strong>" in h
    assert '<a href="https://ozon.ru/search" rel="noopener noreferrer" target="_blank">выдача</a>' in h
    assert "&lt;img src=x onerror=alert(1)&gt;" in h and 'href="javascript' not in h
    assert "Остановить" not in h and "data-live" not in h


@pytest.mark.parametrize(
    "reason,text",
    [
        ("cancelled", "Запуск отменили"),
        ("timeout", "дольше разрешённого"),
        ("lost", "Запуск прервался"),
    ],
)
def test_failed_reasons(client, make_run, reason, text):
    run_id, _ = make_run([], finish={"status": "failed", "reason": reason})
    h = client.get(f"/runs/{run_id}").text
    assert text in h


def test_failed_exit_code(client, make_run):
    run_id, _ = make_run([], finish={"status": "failed", "reason": "exit_code", "exit_code": 2})
    assert "код 2" in client.get(f"/runs/{run_id}").text


def test_unknown_run(client):
    assert client.get("/runs/nope").status_code == 404


def test_markdown_is_safe():
    md = run_screen.markdown
    assert md("<b>x</b>") == "<p>&lt;b&gt;x&lt;/b&gt;</p>"
    assert md("- раз\n- **два**") == "<ul><li>раз</li><li><strong>два</strong></li></ul>"
    assert md("`a*b*c` и *курсив*") == "<p><code>a*b*c</code> и <em>курсив</em></p>"
    assert md("[x](javascript:alert(1))") == "<p>[x](javascript:alert(1))</p>"
    assert md('[x](https://a.ru/"onmouseover=1)') == '<p><a href="https://a.ru/&quot;onmouseover=1" rel="noopener noreferrer" target="_blank">x</a></p>'
    assert md("строка 1\nстрока 2\n\nабзац") == "<p>строка 1<br>строка 2</p><p>абзац</p>"
    assert md(None) == ""


# --- остаточное окно ---------------------------------------------------------------


def test_suspects_warning_and_release(client, make_run, secrets_, kr, monkeypatch):
    run_id, run_dir = make_run([ev("session", action="provisioning")], finish={"status": "failed", "reason": "exit_code", "exit_code": -9})
    (run_dir / "insurance.json").write_text(json.dumps({"suspects": [
        {"session_id": "sus-1", "started_at": "2026-11-14T12:00:01Z"},
        {"session_id": "sus-2", "started_at": "2026-11-14T12:00:02Z"},
    ]}), encoding="utf-8")
    h = client.get(f"/runs/{run_id}").text
    assert "Возможно, остался браузер" in h and "sus-1" in h and "Освободить все (2)" in h

    kr.set_password("dawg-replay", "dawg-api-key", "KEY")
    released = []
    monkeypatch.setattr(insurance, "release_session", lambda key, sid, base: released.append((key, sid)) or True)
    r = post(client, secrets_, f"/runs/{run_id}/release", {"session_id": "sus-1"})
    assert r.status_code == 200 and "Браузер sus-1 освобождён." in r.text
    assert released == [("KEY", "sus-1")]
    assert "sus-2" in r.text and "sus-1</span>" not in r.text
    r = post(client, secrets_, f"/runs/{run_id}/release", {"session_id": "чужая"})
    assert "нет среди подозрительных" in r.text
    r = post(client, secrets_, f"/runs/{run_id}/release")
    assert "Браузер sus-2 освобождён." in r.text and "Возможно, остался браузер" not in r.text


def test_suspects_on_home_and_release_from_there(client, make_run, secrets_, kr, monkeypatch):
    run_id, run_dir = make_run([ev("session", action="provisioning")], finish={"status": "failed", "reason": "exit_code", "exit_code": -9})
    (run_dir / "insurance.json").write_text(json.dumps({"suspects": [
        {"session_id": "sus-9", "started_at": "2026-11-14T12:00:01Z"},
    ]}), encoding="utf-8")
    h = client.get("/").text
    assert "Возможно, остался браузер" in h and "sus-9" in h and f'hx-post="/runs/{run_id}/release"' in h
    kr.set_password("dawg-replay", "dawg-api-key", "KEY")
    monkeypatch.setattr(insurance, "release_session", lambda key, sid, base: True)
    r = post(client, secrets_, f"/runs/{run_id}/release", {"session_id": "sus-9", "_from": "home"})
    assert r.status_code == 200 and r.headers["hx-trigger"] == "runs-changed"
    assert "Браузер sus-9 освобождён." in r.text and "run-suspects" not in r.text
    assert "sus-9" not in client.get("/home/_live").text


def test_running_run_on_home(client, make_run):
    run_id, _ = make_run([ev("stage", name="Собираем карточки"), ev("progress", id="p", label="Страницы", done=2, total=5)])
    h = client.get("/").text
    assert "Идут сейчас" in h and "Собираем карточки, 2 из 5" in h and f'hx-post="/runs/{run_id}/cancel"' in h
    assert 'every 4s' in h  # пока идёт запуск, блок обновляется сам


def test_release_refused_while_running(client, make_run, secrets_):
    run_id, _ = make_run([])
    r = post(client, secrets_, f"/runs/{run_id}/release")
    assert "Запуск ещё идёт" in r.text


# --- отмена и папка ----------------------------------------------------------------


def test_cancel(client, make_run, secrets_, home):
    run_id, _ = make_run([ev("stage", name="A")])
    assert client.post(f"/runs/{run_id}/cancel", headers={"Origin": BASE}).status_code == 403  # без CSRF
    r = post(client, secrets_, f"/runs/{run_id}/cancel")
    assert r.status_code == 200 and "Останавливаем" in r.text and 'id="run-head"' in r.text
    assert r.headers["hx-trigger"] == "runs-changed"  # главная перерисует «Идут сейчас»
    with App(home) as app:
        assert app.runs.cancel_requested(run_id)


def test_cancel_finished_run_is_harmless(client, make_run, secrets_):
    run_id, _ = make_run([], finish={"status": "ok", "reason": None})
    r = post(client, secrets_, f"/runs/{run_id}/cancel")
    assert r.status_code == 200 and "Остановить" not in r.text


def test_open_folder(client, make_run, secrets_, monkeypatch):
    run_id, run_dir = make_run([])
    opened = []
    monkeypatch.setattr(run_screen, "open_folder", opened.append)
    r = post(client, secrets_, f"/runs/{run_id}/open-folder")
    assert "Папка открыта" in r.text and opened == [run_dir / "output"]


# --- скачивание ---------------------------------------------------------------------


def test_download(client, make_run, home, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("секрет", encoding="utf-8")
    run_id, run_dir = make_run([
        ev("artifact", path="cards.csv", label="CSV", mime="text/csv"),
        ev("artifact", path="sub/data.json", label="JSON"),
        ev("artifact", path="link.txt", label="ссылка наружу"),
        ev("artifact", path="gone.csv", label="удалён"),
    ])
    out = run_dir / "output"
    (out / "cards.csv").write_text("sku\n1\n", encoding="utf-8", newline="")
    (out / "sub").mkdir()
    (out / "sub" / "data.json").write_text("{}", encoding="utf-8")
    (out / "unlisted.txt").write_text("x", encoding="utf-8")
    os.symlink(secret, out / "link.txt")
    (run_dir / "stdout.log").write_text("лог", encoding="utf-8")

    r = client.get(f"/runs/{run_id}/files/cards.csv")
    assert r.status_code == 200 and r.text == "sku\n1\n"
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith("attachment;")
    assert client.get(f"/runs/{run_id}/files/sub/data.json").status_code == 200

    for bad in ("../stdout.log", "..%2Fstdout.log", "%2E%2E/stdout.log", "sub/../../stdout.log",
                "link.txt", "unlisted.txt", "gone.csv", "/etc/passwd", "%2Fetc%2Fpasswd"):
        r = client.get(f"/runs/{run_id}/files/{bad}")
        assert r.status_code == 404, bad
        assert "секрет" not in r.text and "лог" != r.text


def test_safe_output_file(tmp_path):
    out = tmp_path / "output"
    (out / "a").mkdir(parents=True)
    (out / "a" / "f.txt").write_text("1")
    (tmp_path / "outside.txt").write_text("2")
    os.symlink(out / "a" / "f.txt", out / "inner-link.txt")
    os.symlink(tmp_path / "outside.txt", out / "outer-link.txt")
    os.symlink(tmp_path, out / "dir-link")
    f = run_screen.safe_output_file
    assert f(out, "a/f.txt") == (out / "a" / "f.txt").resolve()
    assert f(out, "inner-link.txt") == (out / "a" / "f.txt").resolve()  # внутри output можно
    for bad in ("outer-link.txt", "dir-link/outside.txt", "../outside.txt", "a", "", "C:/x", "\\x", "nope"):
        assert f(out, bad) is None, bad


# --- SSE ------------------------------------------------------------------------------


class FakeRequest:
    def __init__(self, stopping, headers=None, query=None, disconnect_after=None):
        self.app = SimpleNamespace(state=SimpleNamespace(ui=SimpleNamespace(stopping=stopping)))
        self.headers = headers or {}
        self.query_params = query or {}
        self._n = 0
        self._after = disconnect_after

    async def is_disconnected(self):
        self._n += 1
        return self._after is not None and self._n > self._after


def parse_sse(chunks):
    events = []
    for chunk in chunks:
        if chunk.startswith((":", "retry")):
            continue
        fields = dict(line.split(": ", 1) for line in chunk.strip().split("\n"))
        events.append((fields.get("event"), int(fields["id"]) if "id" in fields else None, json.loads(fields["data"])))
    return events


def jinja(home, kr):
    from starlette.templating import Jinja2Templates

    from dawg_replay.web import views
    from dawg_replay.web.server import HERE

    t = Jinja2Templates(directory=str(HERE / "templates"))
    views.setup_templates(t, Secrets(port=PORT))
    run_screen.setup(t)
    return t


def stream(view, templates, after, *, headers=None, disconnect_after=None, **kw):
    async def run():
        stopping = asyncio.Event()
        req = FakeRequest(stopping, headers=headers, disconnect_after=disconnect_after)
        a = run_screen._after(req) if headers else after
        return [c async for c in run_screen.run_stream(req, templates, view, a, interval=0.01, **kw)]

    return parse_sse(asyncio.run(run()))


def write_events(run_dir, *events, mode="a"):
    with open(run_dir / "events.jsonl", mode, encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def test_sse_patch_with_ids_and_done(home, kr, make_run):
    t = jinja(home, kr)
    run_id, run_dir = make_run([ev("stage", name="A")])
    state = {"status": "running"}
    view = RunView(run_dir, run_info=lambda: {"id": run_id, "status": state["status"], "label": state["status"], "suspects": []})
    snap = view.snapshot()
    write_events(run_dir, ev("stage", name="B"), ev("progress", id="p", label="P", done=1, total=2),
                 ev("log", level="info", msg="<i>привет</i>"), ev("result", status="ok", summary="готово"))
    state["status"] = "ok"
    events = stream(view, t, snap["seq"], done_after=0.03)
    kinds = [e[0] for e in events]
    assert kinds[0] == "patch" and kinds[-1] == "done"
    patch = events[0]
    assert patch[1] > snap["seq"]
    assert set(patch[2]["panels"]) >= {"trail", "progress", "result", "head"}
    assert "B" in patch[2]["panels"]["trail"] and 'id="run-trail"' in patch[2]["panels"]["trail"]
    assert patch[2]["log"] and "&lt;i&gt;привет&lt;/i&gt;" in patch[2]["log"][0]
    assert "Готово" in patch[2]["panels"]["result"]


def test_sse_last_event_id_resumes(home, kr, make_run):
    t = jinja(home, kr)
    run_id, run_dir = make_run([ev("stage", name="A"), ev("stage", name="B")])
    view = RunView(run_dir, run_info=lambda: {"id": run_id, "status": "running", "label": "running", "suspects": []})
    first = view.snapshot()["seq"]
    write_events(run_dir, ev("log", level="info", msg="после переподключения"))
    # Клиент уже видел всё до first: Last-Event-ID важнее ?after.
    events = stream(view, t, 0, headers={"last-event-id": str(first)}, disconnect_after=3)
    assert len(events) == 1 and events[0][0] == "patch"
    assert len(events[0][2]["log"]) == 1 and "после переподключения" in events[0][2]["log"][0]
    assert "trail" not in events[0][2]["panels"]  # шаги A и B повторно не шлются


@pytest.mark.parametrize("case", ["ahead", "garbage", "too_old", "truncated"])
def test_sse_resync(home, kr, make_run, case):
    t = jinja(home, kr)
    run_id, run_dir = make_run([ev("stage", name="A")])
    view = RunView(run_dir, history=3, run_info=lambda: {"id": run_id, "status": "running", "label": "running", "suspects": []})
    seq = view.snapshot()["seq"]
    headers = None
    if case == "ahead":
        after = seq + 100  # сервер перезапускался, у клиента номер от старого вида
    elif case == "garbage":
        after, headers = 0, {"last-event-id": "abc"}
    elif case == "too_old":
        write_events(run_dir, *[ev("log", level="info", msg=str(i)) for i in range(10)])
        after = seq
    else:
        write_events(run_dir, ev("stage", name="Z"), mode="w")
        after = seq
    events = stream(view, t, after, headers=headers, disconnect_after=5)
    assert events and events[0][0] == "resync"


def test_sse_route_finished_run(client, make_run):
    run_id, _ = make_run([ev("stage", name="A"), ev("result", status="ok", summary="ок")], finish={"status": "ok", "reason": None})
    page = client.get(f"/runs/{run_id}").text
    after = int(page.split('data-after="')[1].split('"')[0])
    r = client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": str(after)})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse([c + "\n\n" for c in r.text.split("\n\n") if c.strip()])
    assert [e[0] for e in events] == ["done"]


def test_sse_route_requires_cookie(home, kr, secrets_, make_run):
    run_id, _ = make_run([])
    app = create_app(home, secrets_, keyring_backend=kr)
    with TestClient(app, base_url=BASE) as c:
        assert c.get(f"/runs/{run_id}/events").status_code == 403


def test_cli_run_shown_live(client, make_run, home):
    """Запуск из CLI: тот же каталог и БД, экран видит его без UI-супервизора."""
    run_id, run_dir = make_run([ev("stage", name="Из CLI")], draft=False)
    h = client.get(f"/runs/{run_id}").text
    assert "Из CLI" in h and "data-live" in h


# --- знак в шапке -------------------------------------------------------------------


def test_brand_mark_animates_while_runs_are_active(client, make_run, home):
    for path in ("/", "/scripts", "/settings"):
        h = client.get(path).text
        assert '<body>' in h and 'class="brand-line"' in h and 'class="brand-dot"' in h
    assert client.get("/events/active").json() == {"running": 0}
    with App(home) as app:
        assert app.runs_fingerprint().startswith("0:")

    run_id, _ = make_run([ev("stage", name="A")])
    for path in ("/", "/settings", f"/runs/{run_id}"):
        assert '<body class="runs-active">' in client.get(path).text, path
    r = client.get("/events/active")
    assert r.json() == {"running": 1} and r.headers["cache-control"] == "no-store"
    with App(home) as app:
        assert app.runs_fingerprint().startswith("1:")
        app.runs.finish(run_id, status="ok", reason=None, exit_code=0)
    assert '<body>' in client.get("/").text
