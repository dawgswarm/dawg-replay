"""Починить с агентом и тихая поломка (dawg_replay.repair), маскировка (diagnosis.scrub),
момент прошлого визита (dawg_replay.uistate)."""

import json

import pytest
from starlette.testclient import TestClient

from dawg_replay import diagnosis, repair, uistate
from dawg_replay.app import App
from dawg_replay.cli import main
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54324
BASE = f"http://127.0.0.1:{PORT}"
API_KEY = "dawg_live_KEY123456"


# --- маскировка ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, hidden, kept",
    [
        ("GET https://api.x.ru/v1/items?token=abc123&page=2", "abc123", "page=2"),
        ("https://x.ru/a?query=ноутбук&api_key=SEKRET99", "SEKRET99", "query=ноутбук"),
        ("https://s3.x/f.csv?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA1%2F2&X-Amz-Date=20261114",
         "deadbeef", "X-Amz-Date=20261114"),
        ("https://x.ru/p?key=K1K2K3&sig=S1S2&id=7", "K1K2K3", "id=7"),
        ("https://x.ru/p?keyword=dress&access_token=T0K3N", "T0K3N", "keyword=dress"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi", "Authorization"),
        ('{"api_key": "abcd-efgh-1234", "page": 3}', "abcd-efgh-1234", '"page": 3'),
        ("DAWG_API_KEY=dk_live_42424242 uv run", "dk_live_42424242", "uv run"),
        ("proxy http://user:hunter22@proxy.ru:8080/", "hunter22", "user:***@proxy.ru"),
        ("password: qwerty12", "qwerty12", "password"),
    ],
)
def test_scrub_hides_tokens(raw, hidden, kept):
    out = diagnosis.scrub(raw)
    assert hidden not in out and diagnosis.MASK in out and kept in out


def test_scrub_keeps_ordinary_urls_and_known_secrets():
    url = "https://www.ozon.ru/search/?text=ноутбук&page=3&sorting=price"
    assert diagnosis.scrub(url) == url
    assert diagnosis.scrub("ключ abcd1234 в тексте", ["abcd1234"]) == "ключ *** в тексте"
    assert diagnosis.sensitive_param("X-Amz-Signature") and diagnosis.sensitive_param("client_secret")
    assert not diagnosis.sensitive_param("keyword") and not diagnosis.sensitive_param("author")


# --- правило тихой поломки ----------------------------------------------------------------


def test_judge_empty_needs_counter_and_no_files():
    assert repair.judge(("rows", 0), False, [])["kind"] == "empty"
    assert repair.judge(("rows", 0), True, []) is None  # файл результата есть
    assert repair.judge(None, False, [100, 100, 100]) is None  # скрипт ничего не считал


def test_judge_drop_threshold():
    hist = [100, 120, 80]  # медиана 100
    assert repair.judge(("rows", 49), True, hist)["kind"] == "drop"
    assert "Собрано 21 строка" in repair.judge(("rows", 21), True, hist)["text"]
    assert "Собрано 11 строк" in repair.judge(("rows", 11), True, hist)["text"]
    assert repair.judge(("rows", 50), True, hist) is None  # ровно половина: ещё не поломка
    assert repair.judge(("rows", 49), True, hist[:2]) is None  # меньше 3 прошлых запусков
    assert repair.judge(("rows", 2), True, [8, 9, 9]) is None  # медиана меньше 10: слишком мало для выводов
    s = repair.judge(("rows", 3), True, [100, 100, 100, 1])
    assert s["median"] == 100 and "обычно около 100" in s["text"] and "Собрано 3 строки" in s["text"]


# --- данные в каталоге приложения ------------------------------------------------------------


def _events(rows=None, progress=None, artifact=None, errors=()):
    ev = []
    if rows is not None:
        ev.append({"t": "rows", "table": "cards", "rows": [{"i": i} for i in range(rows)]})
    if progress is not None:
        ev.append({"t": "progress", "id": "pages", "label": "Страницы", "done": progress, "total": 10})
    if artifact:
        ev.append({"t": "artifact", "path": artifact, "label": "CSV"})
    for e in errors:
        ev.append({"t": "log", "level": "error", "msg": e})
    return ev


@pytest.fixture
def make_run(home, kr, write_script):
    with App(home, keyring_backend=kr) as a:
        a.registry.add(write_script(version=1))
    counter = iter(range(10, 99))

    def make(status="ok", events=(), preset=None, params=None, reason=None, stderr="", draft=False, files=(),
             summary=None, run_id=None, exit_code=0):
        rid = run_id or f"20261114-1200{next(counter):02d}-aaaaaa"
        with App(home, keyring_backend=kr) as a:
            a.runs.reserve(run_id=rid, script_name="ozon-cards", script_version=1, script_path="/x/v1/script.py",
                           draft=draft, preset=preset, params=params if params is not None else {"query": "q"},
                           timeout_seconds=60, limit=100)
            d = a.paths.run_dir(rid)
            (d / "output").mkdir(parents=True, exist_ok=True)
            for f in files:
                (d / "output" / f).write_text("a,b\n1,2\n", encoding="utf-8")
            (d / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                                            encoding="utf-8")
            (d / "stderr.log").write_text(stderr, encoding="utf-8")
            a.runs.finish(rid, status=status, reason=reason, exit_code=exit_code, summary=summary)
        return rid

    return make


def _rec(home, kr, rid):
    a = App(home, keyring_backend=kr)
    return a, a.runs.get(rid)


def test_scan_counts_rows_progress_and_existing_files(make_run, home, kr):
    rid = make_run(events=_events(rows=3, progress=2, artifact="cards.csv") + _events(rows=4), files=["cards.csv"])
    gone = make_run(events=_events(progress=5, artifact="lost.csv") + [{"t": "artifact", "path": "../../etc/passwd"}])
    with App(home, keyring_backend=kr) as a:
        s = repair.scan(a.paths.run_dir(rid))
        assert s.counter() == ("rows", 7) and s.files == ["cards.csv"]
        g = repair.scan(a.paths.run_dir(gone))
        assert g.counter() == ("progress:pages", 5) and g.files == []  # файла нет, путь наружу отброшен
        # кеш по размеру файла: дописали событие, разбор обновился
        with open(a.paths.run_dir(rid) / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": "rows", "table": "cards", "rows": [{"i": 1}]}) + "\n")
        assert repair.scan(a.paths.run_dir(rid)).counter() == ("rows", 8)


def test_suspicion_compares_same_preset_only(make_run, home, kr):
    for _ in range(3):
        make_run(events=_events(rows=100), preset="ноутбуки", files=["c.csv"])
    make_run(events=_events(rows=5), preset="другой", files=["c.csv"])  # другой пресет: истории нет
    make_run(status="partial", events=_events(rows=100), preset="ноутбуки")  # partial в историю не идёт
    make_run(events=_events(rows=100), preset="ноутбуки", draft=True)  # черновой тоже
    bad = make_run(events=_events(rows=7), preset="ноутбуки", files=["c.csv"])
    a, rec = _rec(home, kr, bad)
    with a:
        s = repair.suspicion(a, rec)
        assert s["kind"] == "drop" and s["runs"] == 3 and s["median"] == 100
        other = [r for r in a.runs.list("ozon-cards", 50) if r.preset == "другой"][0]
        assert repair.suspicion(a, other) is None
        assert repair.needs_repair(a, rec) == {"suspicion": s}


def test_suspicion_without_preset_groups_by_params(make_run, home, kr):
    for _ in range(3):
        make_run(events=_events(rows=100), params={"query": "a"}, files=["c.csv"])
    rid = make_run(events=_events(rows=10), params={"query": "b"}, files=["c.csv"])
    a, rec = _rec(home, kr, rid)
    with a:
        assert repair.suspicion(a, rec) is None  # другие параметры: сравнивать не с чем
    rid = make_run(events=_events(rows=10), params={"query": "a"}, files=["c.csv"])
    a, rec = _rec(home, kr, rid)
    with a:
        assert repair.suspicion(a, rec)["kind"] == "drop"


def test_needs_repair_failed_but_not_cancelled(make_run, home, kr):
    failed = make_run(status="failed", reason="exit_code", exit_code=1)
    cancelled = make_run(status="failed", reason="cancelled")
    fine = make_run(events=_events(rows=10), files=["c.csv"])
    with App(home, keyring_backend=kr) as a:
        assert repair.needs_repair(a, a.runs.get(failed)) == {"suspicion": None}
        assert repair.needs_repair(a, a.runs.get(cancelled)) is None
        assert repair.needs_repair(a, a.runs.get(fine)) is None


# --- промпт -------------------------------------------------------------------------


def test_fix_prompt_template_and_masking(make_run, home, kr, monkeypatch):
    monkeypatch.delenv("DAWG_API_KEY", raising=False)
    kr.set_password("dawg-replay", "dawg-api-key", API_KEY)
    with App(home, keyring_backend=kr) as a:
        a.set_secret("ozon-cards", "sheet_token", "SHEETSECRET77")
    stderr = (
        "Traceback (most recent call last):\n"
        f"  requests to https://api.dawg/x?api_key={API_KEY}\n"
        "  got 403 from https://cdn.x.ru/f?token=tok_abcdef&page=1\n"
        "  uploading with SHEETSECRET77\n"
        "TimeoutError: селектор .tile не найден\n"
    )
    rid = make_run(status="failed", reason="exit_code", exit_code=1, stderr=stderr, preset="ноутбуки",
                   params={"query": "ноутбук", "max_pages": 3, "url": "https://x.ru/?sig=SIG999"},
                   events=_events(errors=["сайт не пропустил проверку на странице 2"]), summary="упал на странице 2")
    a, rec = _rec(home, kr, rid)
    with a:
        p = repair.fix_prompt(a, rec)
    t = p["text"]
    for part in (
        "Скрипт dawg-replay сломался", "dawg-replay-builder", "«Ozon: карточки» (ozon-cards), версия 1",
        "/x/v1/script.py", rid, "пресет «ноутбуки»", 'query = "ноутбук"', "max_pages = 3",
        f"dawg-replay logs {rid} --events", "dawg-replay show ozon-cards --json", "dawg-replay add ./ozon-cards/script.py",
        "сайт не пропустил проверку на странице 2", "упал на странице 2", "TimeoutError: селектор .tile не найден", "Скрипт завершился с кодом 1.",
    ):
        assert part in t, part
    for secret in (API_KEY, "SHEETSECRET77", "tok_abcdef", "SIG999"):
        assert secret not in t, secret
    assert "page=1" in t and p["script_path"] == "/x/v1/script.py" and p["suspicion"] is None


def test_fix_prompt_for_silent_breakage_and_running(make_run, home, kr):
    rid = make_run(events=_events(rows=0))
    a, rec = _rec(home, kr, rid)
    with a:
        t = repair.fix_prompt(a, rec)["text"]
        assert "похоже, сайт поменялся" in t and "ничего не собрал" in t and "Собрано строк: 0" in t
        a.runs.reserve(run_id="live", script_name="ozon-cards", script_version=1, script_path="/x", draft=False,
                       preset=None, params={}, timeout_seconds=60, limit=100)
        with pytest.raises(Exception) as e:
            repair.fix_prompt(a, a.runs.get("live"))
        assert getattr(e.value, "code", None) == "run_running"


# --- CLI ----------------------------------------------------------------------------


def test_cli_fix_prompt(make_run, home, kr, capsys):
    kr.set_password("dawg-replay", "dawg-api-key", API_KEY)
    rid = make_run(status="failed", reason="exit_code", exit_code=1, stderr=f"boom with {API_KEY}\n")
    assert main(["fix-prompt", rid]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Скрипт dawg-replay сломался") and API_KEY not in out and "boom with ***" in out
    assert main(["fix-prompt", rid, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and data["run"]["id"] == rid and data["script"] == "ozon-cards" and data["version"] == 1
    assert "dawg-replay-builder" in data["prompt"] and API_KEY not in json.dumps(data)
    assert main(["fix-prompt", "nope", "--json"]) != 0
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "run_not_found"


# --- момент прошлого визита ----------------------------------------------------------------


def test_visit_boundary(tmp_path):
    f = tmp_path / "ui-state.json"
    assert uistate.visit(f, "2026-11-14T10:00:00Z") is None  # первый визит: показывать нечего
    assert uistate.visit(f, "2026-11-14T10:20:00Z") is None  # тот же визит
    assert uistate.visit(f, "2026-11-14T12:00:00Z") == "2026-11-14T10:20:00Z"  # новый визит после перерыва
    assert uistate.visit(f, "2026-11-14T12:10:00Z") == "2026-11-14T10:20:00Z"  # граница не сдвигается внутри визита
    assert uistate.peek(f) == "2026-11-14T10:20:00Z"
    f.write_text("not json", encoding="utf-8")
    assert uistate.peek(f) is None and uistate.visit(f, "2026-11-14T13:00:00Z") is None


# --- веб ----------------------------------------------------------------------------


@pytest.fixture
def client(home, kr):
    s = Secrets(port=PORT)
    with TestClient(create_app(home, s, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={s.token}").status_code == 303
        yield c


def test_run_screen_repair_panel(client, make_run, home, kr):
    kr.set_password("dawg-replay", "dawg-api-key", API_KEY)
    failed = make_run(status="failed", reason="exit_code", exit_code=1, stderr=f"key {API_KEY}\n<b>x</b>\n")
    h = client.get(f"/runs/{failed}").text
    assert 'id="run-repair"' in h and "Починить с агентом" in h and 'data-copy="repair-text"' in h
    assert "Похоже, сайт поменялся" not in h and API_KEY not in h and "&lt;b&gt;x&lt;/b&gt;" in h
    assert f"dawg-replay fix-prompt {failed}" in h

    cancelled = make_run(status="failed", reason="cancelled")
    assert "Починить с агентом" not in client.get(f"/runs/{cancelled}").text

    empty = make_run(events=_events(rows=0))
    h = client.get(f"/runs/{empty}").text
    assert "Похоже, сайт поменялся" in h and "Починить с агентом" in h


def test_home_attention_and_since_last_visit(client, make_run, home, kr):
    with App(home, keyring_backend=kr) as a:
        state = a.paths.home / "ui-state.json"
    # Прошлый визит был давно: всё, что закончилось после него, попадает в блок.
    state.write_text(json.dumps({"last_seen": "2000-01-01T00:00:00Z"}), encoding="utf-8")
    bad = make_run(status="failed", reason="exit_code", exit_code=1, summary="сломалось")
    make_run(status="failed", reason="cancelled")
    empty = make_run(events=_events(rows=0))
    h = client.get("/").text
    since = h.split("С прошлого визита", 1)[1].split("Идут сейчас", 1)[0]
    assert f"/runs/{bad}#repair" in since and f"/runs/{empty}#repair" in since and "похоже, сайт поменялся" in since
    assert since.count("Починить с агентом") == 2  # отменённый не попал
    att = h.split("Требует внимания", 1)[1].split("Последние запуски", 1)[0]
    assert f"/runs/{empty}#repair" in att and "ничего не собрал" in att  # последний запуск скрипта: тихая поломка
    # Тот же визит: граница не сдвинулась, блок остаётся и в живом обновлении.
    assert "С прошлого визита" in client.get("/").text
    assert "С прошлого визита" in client.get("/home/_live").text


def test_home_first_visit_has_no_since_block(client, make_run):
    make_run(status="failed", reason="exit_code", exit_code=1)
    assert "С прошлого визита" not in client.get("/").text
