import sys

import pytest

from dawg_replay.app import App
from dawg_replay.config import load_config
from dawg_replay.db import MIGRATIONS, connect
from dawg_replay.errors import ReplayError
from dawg_replay.values import parse_set_args

EXTRA_SORT = """\
#
# [[params]]
# name = "sort"
# type = "enum"
# title = "Сортировка"
# choices = ["new", "popular"]
"""


def err(fn, *a):
    with pytest.raises(ReplayError) as e:
        fn(*a)
    return e.value


# --- база и каталог ---


def test_layout_and_schema(app, home):
    assert (home / "db.sqlite").exists()
    assert (home / "scripts").is_dir() and (home / "runs").is_dir()
    tables = {r[0] for r in app.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"scripts", "script_versions", "presets", "runs"} <= tables
    assert app.db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_migration_idempotent(home, kr):
    App(home, kr).close()
    conn = connect(home / "db.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    conn.close()


def test_default_home_from_env(home, kr):
    with App(keyring_backend=kr) as a:
        assert a.paths.home == home


# --- реестр ---


def test_add_copies_file_and_registers(app, home, write_script):
    src = write_script(version=3)
    r = app.registry.add(src)
    assert r.status == "added" and r.previous_version is None
    dest = home / "scripts" / "ozon-cards" / "v3" / "script.py"
    assert dest.read_bytes() == src.read_bytes()
    assert r.version.path == dest
    s = app.registry.get("ozon-cards")
    assert (s.current_version, s.title, s.versions_count) == (3, "Ozon: карточки", 1)


def test_add_new_version_becomes_current(app, write_script):
    app.registry.add(write_script(version=1))
    r = app.registry.add(write_script(version=2, title="Ozon v2"))
    assert r.status == "added" and r.previous_version == 1
    assert app.registry.get("ozon-cards").current_version == 2
    assert [v.version for v in app.registry.versions("ozon-cards")] == [1, 2]
    assert app.registry.get("ozon-cards").title == "Ozon v2"


def test_add_same_content_is_noop(app, write_script):
    f = write_script(version=1)
    app.registry.add(f)
    r = app.registry.add(f)
    assert r.status == "unchanged"
    assert app.registry.get("ozon-cards").versions_count == 1


def test_add_same_version_other_content(app, write_script):
    app.registry.add(write_script(version=1))
    e = err(app.registry.add, write_script(version=1, title="другое"))
    assert e.code == "version_exists" and "Поднимите `version`" in e.message


def test_add_lower_version(app, write_script):
    app.registry.add(write_script(version=5))
    e = err(app.registry.add, write_script(version=4))
    assert e.code == "version_too_low" and "version = 6" in e.message


def test_add_invalid_manifest(app, write_script):
    e = err(app.registry.add, write_script(text="print(1)\n"))
    assert e.code == "invalid_manifest"
    assert e.details[0]["code"] == "missing_block"
    assert app.registry.list() == []


def test_add_missing_file(app, tmp_path):
    assert err(app.registry.add, tmp_path / "nope.py").code == "file_not_found"


def test_rollback(app, write_script):
    app.registry.add(write_script(version=1))
    app.registry.add(write_script(version=2))
    v = app.registry.rollback("ozon-cards", 1)
    assert v.version == 1 and v.current
    assert app.registry.manifest("ozon-cards").version == 1
    assert err(app.registry.rollback, "ozon-cards", 7).code == "version_not_found"
    # после отката новая регистрация всё равно должна быть выше максимума
    assert err(app.registry.add, write_script(version=2, title="x")).code == "version_exists"
    assert app.registry.add(write_script(version=3)).version.current


def test_unknown_script(app):
    for fn in (app.registry.get, app.registry.versions, app.show, app.list_presets):
        assert err(fn, "nope").code == "script_not_found"


def test_stored_file_deleted(app, write_script):
    r = app.registry.add(write_script(version=1))
    r.version.path.unlink()
    assert err(app.registry.manifest, "ozon-cards").code == "stored_script_broken"


def test_remove(app, home, kr, write_script):
    app.registry.add(write_script(version=1))
    app.registry.add(write_script(version=2))
    app.save_preset("ozon-cards", "base", {"query": "q"})
    app.set_secret("ozon-cards", "sheet_token", "tok")
    app.db.execute(
        "INSERT INTO runs (id, script_name, script_version, script_path, status, created_at) VALUES ('r1', 'ozon-cards', 2, 'x', 'ok', 'now')"
    )
    r = app.remove("ozon-cards")
    assert r == {"name": "ozon-cards", "versions": [1, 2], "secrets_deleted": ["sheet_token"], "schedules_removed": [],
                 "logins_deleted": 0, "links_removed": [], "in_projects": []}
    assert app.registry.list() == []
    assert not (home / "scripts" / "ozon-cards").exists()
    assert app.db.execute("SELECT COUNT(*) FROM presets").fetchone()[0] == 0
    assert app.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert kr.store == {}


# --- пресеты ---


def test_preset_roundtrip(app, write_script):
    app.registry.add(write_script(version=1))
    p = app.save_preset("ozon-cards", "ноутбуки", {"query": "ноутбук", "max_pages": 3})
    assert p.params == {"query": "ноутбук", "max_pages": 3} and p.dropped == []
    app.save_preset("ozon-cards", "ноутбуки", {"query": "ноутбук"})
    assert app.get_preset("ozon-cards", "ноутбуки").params == {"query": "ноутбук"}
    assert [x.name for x in app.list_presets("ozon-cards")] == ["ноутбуки"]
    app.delete_preset("ozon-cards", "ноутбуки")
    assert app.list_presets("ozon-cards") == []
    assert err(app.delete_preset, "ozon-cards", "ноутбуки").code == "preset_not_found"


def test_preset_partial_is_fine(app, write_script):
    app.registry.add(write_script(version=1))
    assert app.save_preset("ozon-cards", "p", {"max_pages": 2}).params == {"max_pages": 2}


@pytest.mark.parametrize(
    "values, fragment",
    [
        ({"sheet_token": "tok"}, "секрет здесь не хранится"),
        ({"nope": 1}, "такого параметра нет"),
        ({"max_pages": 500}, "больше max"),
        ({"query": 5}, "ожидается строка"),
    ],
)
def test_preset_rejects(app, write_script, values, fragment):
    app.registry.add(write_script(version=1))
    e = err(app.save_preset, "ozon-cards", "p", values)
    assert e.code == "bad_params" and fragment in e.message
    assert app.list_presets("ozon-cards") == []


@pytest.mark.parametrize("name, fragment", [
    ("", "пустое"), ("   ", "пустое"), ("-x", "«-»"), ("a\nb", "непечатный"), ("a\tb", "непечатный"),
    ("a\x00b", "непечатный"), ("x" * 65, "длиннее 64"),
])
def test_preset_bad_name(app, write_script, name, fragment):
    app.registry.add(write_script(version=1))
    e = err(app.save_preset, "ozon-cards", name, {})
    assert e.code == "bad_preset_name" and fragment in e.message
    assert app.list_presets("ozon-cards") == []


@pytest.mark.parametrize("name", ["macbook m5 max", "Москва (центр)", "50% & «скидки» \"best\"", "x" * 64])
def test_preset_free_name(app, write_script, name):
    app.registry.add(write_script(version=1))
    assert app.save_preset("ozon-cards", f"  {name}\u00a0", {"query": "q"}).name == name
    assert [p.name for p in app.list_presets("ozon-cards")] == [name]
    assert app.get_preset("ozon-cards", f" {name} ").params == {"query": "q"}
    app.delete_preset("ozon-cards", name)
    assert app.list_presets("ozon-cards") == []


def test_import_id_with_trailing_newline_is_not_a_file_name(app, tmp_path):
    from dawg_replay.imports import Imports

    imp = Imports(app)
    imp.dir.mkdir(parents=True, exist_ok=True)
    good = "A" * 22
    trap = imp.dir / f"{good}\n.py"
    real_file = sys.platform != "win32"  # на Windows перевод строки в имени файла недопустим
    if real_file:
        trap.write_bytes(b"x")
    with pytest.raises(ReplayError) as e:
        imp.load(good + "\n")
    assert e.value.code == "import_not_found"
    imp.discard(good + "\n")
    if real_file:
        assert trap.exists()


def test_preset_filtered_by_current_version(app, write_script):
    app.registry.add(write_script(version=1, extra_params=EXTRA_SORT))
    app.save_preset("ozon-cards", "p", {"query": "q", "sort": "new"})
    app.registry.add(write_script(version=2))  # sort убран
    p = app.get_preset("ozon-cards", "p")
    assert p.params == {"query": "q"} and p.dropped == ["sort"]
    # данные не потеряны: после отката ключ снова виден
    app.registry.rollback("ozon-cards", 1)
    assert app.get_preset("ozon-cards", "p").params == {"query": "q", "sort": "new"}


def test_preset_value_no_longer_valid_is_dropped(app, write_script):
    app.registry.add(write_script(version=1))
    app.save_preset("ozon-cards", "p", {"max_pages": 50})
    text = (write_script(version=2).read_text(encoding="utf-8")).replace("# max = 100", "# max = 10")
    app.registry.add(write_script(text=text, filename="v2.py"))
    p = app.get_preset("ozon-cards", "p")
    assert p.params == {} and p.dropped == ["max_pages"]


def test_no_secrets_in_db(app, write_script):
    app.registry.add(write_script(version=1))
    app.set_secret("ozon-cards", "sheet_token", "very-secret-token")
    app.save_preset("ozon-cards", "p", {"query": "q"})
    app.db.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(f.read_bytes() for f in app.paths.home.rglob("*") if f.is_file())
    assert b"very-secret-token" not in raw


# --- секреты и ключ ---


def test_api_key(app, kr):
    assert app.secrets.get_api_key() is None
    app.secrets.set_api_key("  key-1 \n")
    assert app.secrets.get_api_key() == "key-1"
    assert app.config_info()["api_key_set"] is True
    assert err(app.secrets.set_api_key, "  ").code == "empty_secret"


def test_secret_set(app, kr, write_script):
    app.registry.add(write_script(version=1))
    assert app.secret_status("ozon-cards") == {"sheet_token": False}
    app.set_secret("ozon-cards", "sheet_token", "tok")
    assert app.secret_status("ozon-cards") == {"sheet_token": True}
    assert app.secrets.get_secret("ozon-cards", "sheet_token") == "tok"
    e = err(app.set_secret, "ozon-cards", "query", "x")
    assert e.code == "not_a_secret" and "sheet_token" in e.message
    assert err(app.set_secret, "ozon-cards", "nope", "x").code == "not_a_secret"


# --- настройки ---


def test_config_defaults_and_set(app):
    assert app.config.max_parallel_runs == 3
    app.set_config("base_url", "https://api.example.ru")
    app.set_config("max_parallel_runs", "5")
    cfg = load_config(app.paths.config)
    assert (cfg.base_url, cfg.max_parallel_runs) == ("https://api.example.ru", 5)
    assert err(app.set_config, "nope", "1").code == "unknown_config_key"
    assert err(app.set_config, "max_parallel_runs", "x").code == "bad_config_value"
    assert err(app.set_config, "base_url", "ftp://x").code == "bad_config_value"


def test_config_bad_file(app):
    app.paths.config.write_text("max_parallel_runs = ", encoding="utf-8")
    assert err(lambda: app.config).code == "bad_config"


# --- --set ---


def test_parse_set_args(app, write_script):
    text = write_script(version=1).read_text(encoding="utf-8").replace(
        "# ///\n\nprint",
        '#\n# [[params]]\n# name = "urls"\n# type = "url_list"\n# title = "U"\n#\n'
        '# [[params]]\n# name = "fast"\n# type = "bool"\n# title = "F"\n# ///\n\nprint',
    )
    app.registry.add(write_script(text=text, filename="v.py"))
    m = app.registry.manifest("ozon-cards")
    got = parse_set_args(
        m, ["query=a=b", "max-pages=7", "urls=https://a.ru", "urls=https://b.ru", "fast=false"]
    )
    assert got == {"query": "a=b", "max_pages": 7, "urls": ["https://a.ru", "https://b.ru"], "fast": False}
    assert parse_set_args(m, ['urls=["https://a.ru","https://c.ru"]'])["urls"] == ["https://a.ru", "https://c.ru"]
    e = err(parse_set_args, m, ["max_pages=x", "nope=1", "query"])
    assert e.code == "bad_params" and len(e.details) == 3


def test_home_can_be_moved(home, kr, write_script, tmp_path):
    with App(home, kr) as a:
        a.registry.add(write_script(version=1))
        assert a.db.execute("SELECT path FROM script_versions").fetchone()[0] == "scripts/ozon-cards/v1/script.py"
    moved = tmp_path / "moved"
    home.rename(moved)
    with App(moved, kr) as a:
        assert a.registry.manifest("ozon-cards").version == 1


@pytest.mark.parametrize("bad", ["..", ".", "", "a/b", "a\\b", "../db", "x..y", "ozon\n", "/etc", "C:x", "a\x00"])
def test_paths_take_one_safe_component(tmp_path, bad):
    from dawg_replay.paths import Paths

    p = Paths(tmp_path)
    for f in (p.script_dir, p.run_dir, lambda n: p.script_file(n, 1)):
        with pytest.raises(ValueError):
            f(bad)
    assert p.script_dir("ozon-cards") == tmp_path / "scripts" / "ozon-cards"
    assert p.run_dir("20261115-010000-aaaaaa") == tmp_path / "runs" / "20261115-010000-aaaaaa"
