"""Тип параметра file и секретный файл (контракт 1.3, dr-84): kit, CLI, форма, пресеты, история."""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dawg_replay import launch
from dawg_replay.app import App
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app
from dawg_replay_kit.manifest import ManifestError, check_value, parse_manifest
from dawg_replay_kit.parameters import ParamsError, from_cli

POSIX = sys.platform != "win32"
KEY_TEXT = "-----BEGIN PRIVATE KEY-----\nSECRETKEYBODY42\n-----END PRIVATE KEY-----\n"

SCRIPT = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "docs"
# title = "Документы"
# version = 1
#
# [[params]]
# name = "invoice"
# type = "file"
# title = "Счёт"
# required = true
# accept = [".pdf", "image/*"]
# max_mb = 1
#
# [[params]]
# name = "photos"
# type = "file"
# title = "Фото"
# multiple = true
# max_items = 3
#
# [[params]]
# name = "key"
# type = "file"
# title = "Ключ сервиса"
# secret = true
# ///

import json
import os
import dawg_replay_kit as kit

p = kit.params
seen = {"invoice": p.invoice, "photos": p.get("photos"), "key": p.get("key")}
seen["invoice_bytes"] = open(p.invoice["path"], "rb").read().decode("latin-1")
if p.get("key"):
    seen["key_text"] = open(p.key["path"], encoding="utf-8").read()
    seen["key_dir_mode"] = oct(os.stat(os.path.dirname(os.path.dirname(os.path.dirname(p.key["path"])))).st_mode & 0o777)
(kit.output_dir / "seen.json").write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
kit.result("ok", "ok")
'''


def manifest_text(param: str, contract: str = '"1.3"') -> str:
    return ("# /// script\n# dependencies = []\n# ///\n\n# /// dawg-script\n"
            f"# contract = {contract}\n# name = \"m\"\n# title = \"M\"\n# version = 1\n#\n# [[params]]\n"
            + "".join(f"# {line}\n" for line in param.splitlines()) + "# ///\n")


# --- kit -------------------------------------------------------------------------


def test_manifest_file_fields_and_errors():
    m = parse_manifest(manifest_text('name = "f"\ntype = "file"\ntitle = "F"\naccept = [".pdf", "image/*"]\n'
                                     'max_mb = 2.5\nmultiple = true\nmax_items = 4\nsecret = true'))
    (p,) = m.params
    assert (p.accept, p.max_mb, p.multiple, p.max_items, p.is_secret) == ((".pdf", "image/*"), 2.5, True, 4, True)
    assert p.size_limit == int(2.5 * 1024 * 1024) and p.to_dict()["accept"] == [".pdf", "image/*"]
    for param, fragment in [
        ('name = "f"\ntype = "file"\ntitle = "F"\naccept = ["pdf"]', "accept"),
        ('name = "f"\ntype = "file"\ntitle = "F"\nmax_mb = 0', "max_mb"),
        ('name = "f"\ntype = "file"\ntitle = "F"\nmax_items = 2', "max_items"),
        ('name = "f"\ntype = "file"\ntitle = "F"\ndefault = "x"', "default"),
        ('name = "f"\ntype = "string"\ntitle = "F"\naccept = [".pdf"]', "accept"),
        ('name = "f"\ntype = "string"\ntitle = "F"\nmultiple = true', "multiple"),
    ]:
        with pytest.raises(ManifestError) as e:
            parse_manifest(manifest_text(param))
        assert fragment in str(e.value), param
    with pytest.raises(ManifestError) as e:
        parse_manifest(manifest_text('name = "f"\ntype = "file"\ntitle = "F"', contract='"1.2"'))
    assert "1.3" in str(e.value)


def test_check_value_accept_size_count():
    (p,) = parse_manifest(manifest_text('name = "f"\ntype = "file"\ntitle = "F"\naccept = [".pdf", "image/*"]\nmax_mb = 1')).params
    ok = {"path": "/x/a.PDF", "name": "a.PDF", "size": 10}
    assert check_value(p, ok) is None
    assert check_value(p, {**ok, "name": "photo.jpg"}) is None
    assert "не подходит" in check_value(p, {**ok, "name": "a.docx"})
    assert "больше 1 МБ" in check_value(p, {**ok, "size": 1024 * 1024 + 1})
    assert check_value(p, [ok]) is not None and check_value(p, {"name": "a.pdf"}) is not None
    (m,) = parse_manifest(manifest_text('name = "f"\ntype = "file"\ntitle = "F"\nmultiple = true\nmax_items = 2')).params
    assert check_value(m, [ok, ok]) is None and "max_items = 2" in check_value(m, [ok, ok, ok])
    assert "файлов нет" in check_value(m, []) or check_value(m, []) is not None


def test_autonomous_cli_paths(tmp_path):
    a = tmp_path / "счёт.pdf"
    a.write_bytes(b"%PDF")
    m = parse_manifest(manifest_text('name = "doc"\ntype = "file"\ntitle = "D"\nrequired = true'))
    params = from_cli(m, ["--doc", str(a)], {})
    assert params.doc == {"path": str(a.resolve()), "name": "счёт.pdf", "size": 4}
    with pytest.raises(ParamsError) as e:
        from_cli(m, [], {})
    assert "--doc путь/к/файлу" in str(e.value)
    with pytest.raises(ParamsError):
        from_cli(m, ["--doc", str(tmp_path / "нет.pdf")], {})
    mm = parse_manifest(manifest_text('name = "docs"\ntype = "file"\ntitle = "D"\nmultiple = true'))
    assert [f["name"] for f in from_cli(mm, ["--docs", str(a), str(a)], {}).docs] == ["счёт.pdf", "счёт.pdf"]


# --- приложение ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def docs(app, tmp_path):
    f = tmp_path / "docs.py"
    f.write_text(SCRIPT, encoding="utf-8")
    app.registry.add(f)
    return app


@pytest.fixture
def inputs(tmp_path):
    inv = tmp_path / "Счёт 15.pdf"
    inv.write_bytes(b"%PDF-1.4 invoice")
    key = tmp_path / "service.pem"
    key.write_text(KEY_TEXT, encoding="utf-8")
    ph = tmp_path / "фото.jpg"
    ph.write_bytes(b"\xff\xd8\xff jpg")
    return inv, key, ph


def wait_run(home, run_id):
    for _ in range(400):
        with App(home) as a:
            rec = a.runs.get(run_id)
        if not rec.running:
            return rec
        time.sleep(0.05)
    raise AssertionError("запуск не закончился")


def no_secret_anywhere(home):
    for path in Path(home).rglob("*"):
        # seen.json пишет сам тестовый скрипт (он и есть получатель секрета); загрузки и пресеты хранят файл намеренно.
        if path.is_file() and "preset-tables" not in path.parts and "uploads" not in path.parts and path.name != "seen.json":
            assert b"SECRETKEYBODY42" not in path.read_bytes(), path


def test_cli_run_copies_files_secret_only_in_temp(docs, cli, home, inputs):
    inv, key, ph = inputs
    code, data = cli("run", "docs", "--set", f"invoice=@{inv}", "--set", f"photos=@{ph}", "--set", f"photos=@{ph}",
                     "--set", f"key=@{key}", "--follow")
    assert code == 0, data
    run_id = data["run"]["id"]
    run_dir = Path(home) / "runs" / run_id
    seen = json.loads((run_dir / "output" / "seen.json").read_text(encoding="utf-8"))
    assert seen["invoice"]["name"] == "Счёт 15.pdf" and seen["invoice_bytes"] == "%PDF-1.4 invoice"
    assert Path(seen["invoice"]["path"]).parent == (run_dir / "input" / "invoice").resolve()
    assert [f["name"] for f in seen["photos"]] == ["фото.jpg", "фото.jpg"]
    assert len({f["path"] for f in seen["photos"]}) == 2  # одинаковые имена не затирают друг друга
    assert seen["key"]["name"] == "service.pem" and seen["key_text"] == KEY_TEXT
    if POSIX:
        assert seen["key_dir_mode"] == "0o700"
    assert not Path(seen["key"]["path"]).exists()  # временный каталог секретов удалён после выхода
    assert not Path(seen["key"]["path"]).resolve().is_relative_to(run_dir.resolve()) if sys.version_info >= (3, 9) else True
    with App(home) as app:
        hist = app.runs.get(run_id).params
    assert hist["key"] == [{"name": "service.pem"}]
    assert hist["invoice"]["path"] == seen["invoice"]["path"]
    no_secret_anywhere(home)


def test_cli_file_errors(docs, cli, inputs, tmp_path):
    inv, _, _ = inputs
    big = tmp_path / "big.pdf"
    big.write_bytes(b"0" * (1024 * 1024 + 1))
    bad = tmp_path / "x.docx"
    bad.write_bytes(b"x")
    for value, fragment in [(f"invoice={inv}", "имя=@путь"), (f"invoice=@{tmp_path / 'нет.pdf'}", "нет"),
                            (f"invoice=@{big}", "больше 1 МБ"), (f"invoice=@{bad}", "не подходит")]:
        code, data = cli("run", "docs", "--set", value)
        assert code != 0 and fragment in data["error"]["message"], (value, data)


def test_preset_keeps_files_and_secret_separately(docs, cli, home, inputs):
    inv, key, _ = inputs
    code, data = cli("preset", "save", "docs", "счета", "--set", f"invoice=@{inv}", "--set", f"key=@{key}")
    assert code == 0, data
    p = data["preset"]
    assert p["file_params"] == {"invoice": [{"name": "Счёт 15.pdf", "size": 16}], "key": [{"name": "service.pem", "size": len(KEY_TEXT)}]}
    assert "invoice" not in p["params"] and "key" not in p["params"]
    stored = list((Path(home) / "preset-tables" / "docs").glob("*.bin"))
    assert len(stored) == 2
    if POSIX:
        assert all(f.stat().st_mode & 0o777 == 0o600 for f in stored)
    inv.unlink()
    key.unlink()  # пресет не зависит от исходных файлов
    code, data = cli("run", "docs", "--preset", "счета", "--follow")
    assert code == 0, data
    seen = json.loads((Path(home) / "runs" / data["run"]["id"] / "output" / "seen.json").read_text(encoding="utf-8"))
    assert seen["invoice_bytes"] == "%PDF-1.4 invoice" and seen["key_text"] == KEY_TEXT
    no_secret_anywhere(home)
    code, _ = cli("preset", "delete", "docs", "счета")
    assert code == 0 and not list((Path(home) / "preset-tables" / "docs").glob("*.bin"))


def test_repair_prompt_and_rerun_show_secret_file_by_name_only(docs, cli, home, inputs):
    from dawg_replay import repair

    inv, key, _ = inputs
    code, data = cli("run", "docs", "--set", f"invoice=@{inv}", "--set", f"key=@{key}", "--follow")
    run_id = data["run"]["id"]
    with App(home) as app:
        text = repair.fix_prompt(app, app.runs.get(run_id))["text"]
    assert "файл «service.pem»" in text and "файл «Счёт 15.pdf»" in text and "SECRETKEYBODY42" not in text


# --- веб ---------------------------------------------------------------------------------

PORT = 54371
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def web(home, kr, docs):
    secrets_ = Secrets(port=PORT)
    with TestClient(create_app(home, secrets_, keyring_backend=kr), base_url=BASE, follow_redirects=False) as c:
        assert c.get(f"/?token={secrets_.token}").status_code == 303
        yield c, secrets_


def test_web_upload_run_preset_override(web, home):
    c, s = web
    h = {"Origin": BASE, "X-CSRF-Token": s.csrf}
    page = c.get("/scripts/docs").text
    assert 'hx-post="/scripts/docs/_file/invoice"' in page and 'accept=".pdf,image/*"' in page
    assert 'name="_file_upload" type="file" multiple' in page and "Не больше 1 МБ" in page

    r = c.post("/scripts/docs/_file/invoice", files={"_file_upload": ("Счёт.pdf", b"%PDF one", "application/pdf")}, headers=h)
    assert "<strong>Счёт.pdf</strong>" in r.text
    inv = r.text.split('name="invoice" value="', 1)[1].split('"', 1)[0]
    r = c.post("/scripts/docs/_file/key", files={"_file_upload": ("k.pem", KEY_TEXT.encode(), "text/plain")}, headers=h)
    assert "<strong>k.pem</strong>" in r.text and "Файл секретный" in r.text and "SECRETKEYBODY42" not in r.text
    key = r.text.split('name="key" value="', 1)[1].split('"', 1)[0]
    r = c.post("/scripts/docs/_file/invoice", files={"_file_upload": ("x.docx", b"x", "application/octet-stream")}, headers=h)
    assert "не подходит" in r.text and 'name="invoice" value=""' in r.text
    r = c.post("/scripts/docs/_file/invoice", files={"_file_upload": ("big.pdf", b"0" * (1024 * 1024 + 10), "application/pdf")}, headers=h)
    assert "больше 1 МБ" in r.text
    r = c.post("/scripts/docs/_file/invoice", files=[("_file_upload", ("a.pdf", b"a", "application/pdf")),
                                                     ("_file_upload", ("b.pdf", b"b", "application/pdf"))], headers=h)
    assert "один файл" in r.text
    assert c.post("/scripts/docs/_file/nope", files={"_file_upload": ("a.pdf", b"a", "application/pdf")}, headers=h).status_code == 404

    r = c.post("/scripts/docs/presets", data={"_preset_name": "счета", "invoice": inv, "key": key}, headers=h)
    assert "Пресет «счета» сохранён." in r.text
    form = c.get("/scripts/docs/_form", params={"_preset": "счета"}).text
    assert "<strong>Счёт.pdf</strong>" in form and "<strong>k.pem</strong>" in form

    # Замена файла пресета на один запуск: пресет остаётся прежним.
    r = c.post("/scripts/docs/_file/invoice", files={"_file_upload": ("Другой.pdf", b"%PDF two", "application/pdf")}, headers=h)
    other = r.text.split('name="invoice" value="', 1)[1].split('"', 1)[0]
    r = c.post("/scripts/docs/run", data={"_preset": "счета", "invoice": other, "key": key}, headers=h)
    assert r.status_code == 204, r.text
    run_id = r.headers["hx-redirect"].rsplit("/", 1)[1]
    assert wait_run(home, run_id).label == "ok"
    seen = json.loads((Path(home) / "runs" / run_id / "output" / "seen.json").read_text(encoding="utf-8"))
    assert (seen["invoice"]["name"], seen["invoice_bytes"], seen["key_text"]) == ("Другой.pdf", "%PDF two", KEY_TEXT)
    with App(home) as app:
        assert app.get_preset("docs", "счета").file_params["invoice"][0]["name"] == "Счёт.pdf"
    no_secret_anywhere(home)

    r = c.post("/scripts/docs/run", data={"invoice": "", "key": key}, headers=h)
    assert "загрузите файл" in r.text
