"""«Перезапустить упавшие» (dawg_replay.retry): строки таблицы с failed и недошедшие."""

import json
import sys
import time

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch, repair, retry
from dawg_replay.app import App
from dawg_replay.uploads import Uploads
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

PORT = 54334
BASE = f"http://127.0.0.1:{PORT}"


def script(name="acts", secret=False, extra=""):
    return f'''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.2,<2"]
# ///

# /// dawg-script
# contract = "1.2"
# name = "{name}"
# title = "Действия"
# version = 1
#
# [[params]]
# name = "accounts"
# type = "table"
# title = "Аккаунты"
# required = true
# columns = ["email"]
{"# secret = true" if secret else "#"}
#
# [[params]]
# name = "note"
# type = "string"
# title = "Пометка"
{extra}# ///

import json, sys
import dawg_replay_kit as kit

rows = kit.params.accounts
(kit.output_dir / "seen.json").write_text(json.dumps([r["email"] for r in rows]), encoding="utf-8")
for r in rows:
    if r["email"].startswith("stop"):
        sys.exit(1)  # запуск обрывается: до остальных строк не дошёл
    if r["email"].startswith("bad"):
        kit.item(r["email"], "failed", "не вышло", row=r["_row"])
    elif r["email"].startswith("done"):
        kit.item(r["email"], "skipped", "уже сделано", row=r["_row"])
    else:
        kit.item(r["email"], "ok", row=r["_row"])
'''


CSV = "email\na@x.ru\nbad1@x.ru\ndone@x.ru\nbad2@x.ru\nb@x.ru\n"


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


def add(app, tmp_path, **kw):
    f = tmp_path / f"{kw.get('name', 'acts')}.py"
    f.write_text(script(**kw), encoding="utf-8")
    app.registry.add(f)


def run_cli(cli, tmp_path, csv=CSV, name="acts"):
    f = tmp_path / "accounts.csv"
    f.write_text(csv, encoding="utf-8")
    code, data = cli("run", name, "--set", f"accounts=@{f}", "--set", "note=ноябрь", "--follow")
    return data["run"]["id"]


def seen(home, run_id):
    with App(home) as a:
        return json.loads((a.paths.run_dir(run_id) / "output" / "seen.json").read_text("utf-8"))


def wait(home, run_id):
    for _ in range(400):
        with App(home) as a:
            rec = a.runs.get(run_id)
        if not rec.running:
            return rec
        time.sleep(0.05)
    raise AssertionError("запуск не закончился")


class SyncRuns:
    """Как UiRuns.start, но запуск идёт до конца в этом же потоке."""

    def __init__(self, app):
        self.app = app

    def start(self, target, *, preset=None, values=None):
        prepared = launch.prepare(self.app, target, preset=preset, values=values)
        return launch.supervise(self.app, prepared.run.id, prepared.payload())


def test_plan_failed_rows_only(app, tmp_path, cli, home):
    add(app, tmp_path)
    rid = run_cli(cli, tmp_path)
    with App(home) as a:
        rec = a.runs.get(rid)
        p = retry.plan(a, rec)
        assert p["failed"] == 2 and p["unreached"] == 0 and not p["secret"]
        assert [r["email"] for r in p["rows"]] == ["bad1@x.ru", "bad2@x.ru"]
        assert [r["_row"] for r in p["rows"]] == [3, 5]
        run = retry.start(SyncRuns(a), a, rec)
        assert run.preset is None and run.params["note"] == "ноябрь"
    assert seen(home, run.id) == ["bad1@x.ru", "bad2@x.ru"]
    with App(home) as a:
        again = retry.plan(a, a.runs.get(run.id))  # перезапуск перезапуска: те же две
        assert [r["_row"] for r in again["rows"]] == [3, 5]


def test_plan_includes_unreached_when_run_broke_off(app, tmp_path, cli, home):
    add(app, tmp_path)
    rid = run_cli(cli, tmp_path, csv="email\na@x.ru\nbad@x.ru\nstop@x.ru\nc@x.ru\n")
    with App(home) as a:
        rec = a.runs.get(rid)
        assert rec.status == "failed"
        p = retry.plan(a, rec)
        assert p["failed"] == 1 and p["unreached"] == 2
        assert [r["email"] for r in p["rows"]] == ["bad@x.ru", "stop@x.ru", "c@x.ru"]
        # В списках итог от размера таблицы: до двух строк скрипт не дошёл.
        (d,) = repair.annotate(a, [rec.to_dict()])
        assert d["items"] == {"ok": 1, "failed": 1, "skipped": 0, "total": 4}


def test_no_plan_without_failures_rows_or_single_table(app, tmp_path, cli, home):
    add(app, tmp_path)
    ok = run_cli(cli, tmp_path, csv="email\na@x.ru\ndone@x.ru\n")
    add(app, tmp_path, name="two", extra='#\n# [[params]]\n# name = "more"\n# type = "table"\n# title = "Ещё"\n')
    two = run_cli(cli, tmp_path, name="two")
    with App(home) as a:
        assert retry.plan(a, a.runs.get(ok)) is None
        assert retry.plan(a, a.runs.get(two)) is None
        with pytest.raises(Exception, match="перезапускать нечего"):
            retry.start(SyncRuns(a), a, a.runs.get(ok))


def test_no_plan_when_items_have_no_row(app, tmp_path, cli, home):
    add(app, tmp_path)
    rid = run_cli(cli, tmp_path)
    with App(home) as a:
        ev = a.paths.run_dir(rid) / "events.jsonl"
        lines = [json.loads(x) for x in ev.read_text("utf-8").splitlines()]
        ev.write_text("".join(json.dumps({k: v for k, v in e.items() if k != "row"}) + "\n" for e in lines), "utf-8")
        assert retry.plan(a, a.runs.get(rid)) is None


# --- веб: кнопка, секретная таблица ---


@pytest.fixture
def web(home, kr, app, tmp_path):
    add(app, tmp_path, secret=True)
    s = Secrets(port=PORT)
    with TestClient(create_app(home, s, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={s.token}").status_code == 303
        yield c, {"Origin": BASE, "X-CSRF-Token": s.csrf}


def _form_run(c, h):
    r = c.post("/scripts/acts/_table/accounts", files={"_table_file": ("Аккаунты.csv", CSV.encode(), "text/csv")}, headers=h)
    uid = r.text.split('name="accounts" value="', 1)[1].split('"', 1)[0]
    r = c.post("/scripts/acts/run", data={"accounts": uid, "note": "x"}, headers=h)
    assert r.status_code == 204, r.text
    return r.headers["hx-redirect"].rsplit("/", 1)[1]


def test_web_secret_table_retry_and_expiry(web, home):
    c, h = web
    rid = _form_run(c, h)
    wait(home, rid)
    with App(home) as a:
        assert "accounts" not in a.runs.get(rid).params  # секретная таблица не в истории
        assert not list(a.paths.run_dir(rid).rglob("*bad1*"))
    page = c.get(f"/runs/{rid}").text
    # Ключи секретной таблицы kit маскирует; строки различаются по row.
    assert "Перезапустить упавшие (2)" in page and "bad1@x.ru" not in page and "№&nbsp;5" in page
    assert "3 из 5 сделаны" not in page and "2 из 5 сделаны" in page

    r = c.post(f"/runs/{rid}/retry-failed", headers=h)
    assert r.status_code == 204, r.text
    new = r.headers["hx-redirect"].rsplit("/", 1)[1]
    wait(home, new)
    assert seen(home, new) == ["bad1@x.ru", "bad2@x.ru"]
    with App(home) as a:
        assert retry.plan(a, a.runs.get(new))["rows"] is not None  # у перезапуска своя ссылка на таблицу

        # Загрузка устарела: кнопки нет, просьба загрузить файл заново.
        for f in a.paths.uploads.glob("*.json"):
            f.unlink()
    page = c.get(f"/runs/{rid}").text
    assert "Перезапустить упавшие" not in page and "Загрузите файл заново" in page
    r = c.post(f"/runs/{rid}/retry-failed", headers=h)
    assert r.status_code == 200 and "уже не хранится" in r.text


def test_web_retry_needs_csrf(web, home):
    c, h = web
    rid = _form_run(c, h)
    wait(home, rid)
    r = c.post(f"/runs/{rid}/retry-failed", headers={"Origin": BASE})
    assert r.status_code == 403


def test_uploads_link_ignores_bad_ids(tmp_path):
    up = Uploads(tmp_path / "uploads")
    up.link("../x", {"a": "A" * 22})
    up.link("r1", {"a": "not-an-id"})
    assert not (tmp_path / "uploads").exists()
    uid = up.put("a", "f.csv", [{"x": "1", "_row": 2}], secret=True)
    up.link("r1", {"a": uid})
    assert up.linked("r1", "a")["rows"] == [{"x": "1", "_row": 2}]
    assert up.linked("r1", "b") is None and up.linked("r2", "a") is None
