"""Стандартная библиотека узлов (dr-116): установка с приложением и каждый узел в деле.

Без браузера, сети, ключа DAWG, системного планировщика и хранилища паролей: узлы запускаются
интерпретатором тестов через launch.prepare и launch.supervise, дом приложения временный.
"""

import csv
import hashlib
import json
import shutil
import sys

import pytest
from starlette.testclient import TestClient

from dawg_replay import builtin, launch, repair
from dawg_replay.app import App
from dawg_replay.cli import main
from dawg_replay.errors import ReplayError
from dawg_replay.web import home as home_data
from dawg_replay.web.security import Secrets
from dawg_replay.web.server import create_app

NODES = ["std-filter-rows", "std-if", "std-merge-tables", "std-select-columns"]

# Содержимое каждой версии узла неизменно: приложение ставит новую версию, только если
# поднят `version` в манифесте. Правишь узел: подними version и обнови отпечаток здесь.
KNOWN = {
    "std-filter-rows.py": (1, "24467286b4cd6c62eb188d34ce64ce0ccc3c11d23acb84216ba0d1284b9dbd9a"),
    "std-if.py": (1, "bcb8efd556dcfbce34825744f350d6b98cfcefc69c2bcf5a0644be0834c8f905"),
    "std-merge-tables.py": (1, "2145dcd468064e200596a5b32b2d8d930f16a2061206ce6f7409be43ed8fb4ab"),
    "std-select-columns.py": (1, "368498e24920437f452bc8a2bdd65a07d06f7a9257f8984a30d6956272511b42"),
}


@pytest.fixture(autouse=True)
def stdlib_on(monkeypatch):
    monkeypatch.delenv(builtin.ENV_STDLIB, raising=False)
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Своя копия библиотеки: тест меняет её, как новое или старое приложение."""
    d = tmp_path / "bundle"
    shutil.copytree(builtin.STDLIB_DIR, d, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(builtin, "STDLIB_DIR", d)
    return d


def bump(file, version, note):
    text = file.read_text(encoding="utf-8")
    old = next(line for line in text.splitlines() if line.startswith("# version = "))
    file.write_text(text.replace(old, f"# version = {version}", 1) + f"# {note}\n", encoding="utf-8")


# --- установка -------------------------------------------------------------------------------


def test_bundled_versions_are_pinned():
    from dawg_replay_kit.manifest import load_manifest

    got = {}
    for f in builtin.bundled():
        got[f.name] = (load_manifest(f).version, hashlib.sha256(f.read_bytes()).hexdigest())
    assert got == KNOWN


def test_every_node_is_a_plain_script_without_browser():
    from dawg_replay_kit.manifest import load_manifest

    for f in builtin.bundled():
        m = load_manifest(f)
        assert m.name == f.stem and m.name in NODES
        assert m.contract_minor == 4 and m.outputs, m.name
        text = f.read_text(encoding="utf-8")
        assert "kit.browser" not in text and "secret" not in text


def test_first_start_registers_nodes(home, kr):
    with App(home, keyring_backend=kr) as app:
        scripts = app.registry.list()
        assert [s.name for s in scripts] == NODES
        assert all(s.builtin and s.current_version == 1 for s in scripts)
        assert builtin.stored_fingerprint(app) == builtin.fingerprint(builtin.bundled())
        assert builtin.sync(app) is None  # тот же отпечаток: ничего не делает
    with App(home, keyring_backend=kr) as app:
        assert [v.version for v in app.registry.versions("std-if")] == [1]


def test_update_adds_version_and_keeps_user_scripts(home, kr, bundle, write_script):
    with App(home, keyring_backend=kr) as app:
        app.registry.add(write_script())  # скрипт пользователя
    bump(bundle / "std-if.py", 2, "новая версия приложения")
    with App(home, keyring_backend=kr) as app:
        assert [v.version for v in app.registry.versions("std-if")] == [1, 2]
        assert app.registry.get("std-if").current_version == 2
        assert app.registry.get("std-filter-rows").current_version == 1
        user = app.registry.get("ozon-cards")
        assert not user.builtin and user.current_version == 1


def test_downgrade_returns_to_known_version(home, kr, bundle):
    original = (bundle / "std-if.py").read_bytes()
    with App(home, keyring_backend=kr):
        pass
    bump(bundle / "std-if.py", 2, "новее")
    with App(home, keyring_backend=kr) as app:
        assert app.registry.get("std-if").current_version == 2
    (bundle / "std-if.py").write_bytes(original)  # откат приложения на прошлую версию
    with App(home, keyring_backend=kr) as app:
        assert app.registry.get("std-if").current_version == 1


def test_same_version_other_content_is_skipped(home, kr, bundle):
    with App(home, keyring_backend=kr) as app:
        f = bundle / "std-if.py"
        f.write_text(f.read_text(encoding="utf-8") + "# правка без поднятия версии\n", encoding="utf-8")
        report = builtin.sync(app)
        assert report["skipped"] == [{"name": "std-if", "reason": report["skipped"][0]["reason"]}]
        assert "уже зарегистрирована" in report["skipped"][0]["reason"]
        assert sorted(report["unchanged"]) == ["std-filter-rows", "std-merge-tables", "std-select-columns"]


def test_user_script_with_same_name_untouched(home, kr, write_script, monkeypatch):
    monkeypatch.setenv(builtin.ENV_STDLIB, "off")
    with App(home, keyring_backend=kr) as app:
        app.registry.add(write_script(name="std-if", title="Моё если"))
    monkeypatch.delenv(builtin.ENV_STDLIB)
    with App(home, keyring_backend=kr) as app:
        s = app.registry.get("std-if")
        assert not s.builtin and s.title == "Моё если"
        report = builtin.sync(app, force=True)
        assert report["skipped"][0]["name"] == "std-if" and "пользователя" in report["skipped"][0]["reason"]


def test_user_cannot_add_over_builtin(app, write_script):
    with pytest.raises(ReplayError) as e:
        app.registry.add(write_script(name="std-filter-rows", version=5))
    assert e.value.code == "builtin_script"
    assert app.registry.get("std-filter-rows").current_version == 1


def test_removed_node_comes_back_with_update(home, kr, bundle):
    with App(home, keyring_backend=kr) as app:
        app.remove("std-if")
    with App(home, keyring_backend=kr) as app:
        assert "std-if" not in [s.name for s in app.registry.list()]  # тот же выпуск: удалённое не возвращается
    bump(bundle / "std-merge-tables.py", 2, "обновление")
    with App(home, keyring_backend=kr) as app:
        assert app.registry.get("std-if").builtin


def test_cli_list_marks_builtin(home, kr, capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "std-if  v1  Если  [встроенный]" in out
    assert main(["--json", "list"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {s["name"]: s["builtin"] for s in data["scripts"]} == {n: True for n in NODES}


def test_library_filter(app, write_script):
    app.registry.add(write_script())
    lib = home_data.library(app)
    assert [c["script"]["name"] for c in lib["cards"]] == ["ozon-cards"]
    assert lib["total"] == 1 and lib["builtin_count"] == 4
    lib = home_data.library(app, builtin=True)
    assert [c["script"]["name"] for c in lib["cards"]] == NODES and lib["sites"] == []
    assert [c["script"]["name"] for c in home_data.library(app, "склеить", builtin=True)["cards"]] == ["std-merge-tables"]


def test_library_page_chip(home, kr):
    secrets = Secrets(port=54321)
    web = create_app(home, secrets, keyring_backend=kr)
    with TestClient(web, base_url="http://127.0.0.1:54321", follow_redirects=False) as c:
        assert c.get(f"/?token={secrets.token}").status_code == 303
        html = c.get("/scripts").text
        assert "Скриптов пока нет" in html and 'href="/scripts?builtin=1"' in html and "Встроенные узлы" in html
        html = c.get("/scripts?builtin=1").text
        assert "Склеить таблицы" in html and "Отфильтровать строки" in html and "Скриптов пока нет" not in html
        html = c.get("/scripts/std-if").text
        assert "встроенный узел: обновляется вместе с приложением" in html


# --- узлы в деле --------------------------------------------------------------------------------


def run(app, name, **values):
    prepared = launch.prepare(app, name, values=values)
    assert not any("ключ DAWG" in w for w in prepared.warnings)
    rec = launch.supervise(app, prepared.run.id, prepared.payload())
    rec = app.runs.get(rec.id)
    out = app.paths.run_dir(rec.id) / "output"
    files = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
    result = None
    for line in (app.paths.run_dir(rec.id) / "events.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("t") == "result":
            result = e
    return rec, files, result, out


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def table(*rows):
    head, *body = rows
    return [{**dict(zip(head, r)), "_row": n} for n, r in enumerate(body, start=2)]


PRICES = table(
    ("sku", "name", "price"),
    ("1", "Чайник", "1 200,50"),
    ("2", "Кружка", "300"),
    ("3", "Ложка", ""),
    ("4", "чайник большой", "2500"),
)


@pytest.mark.parametrize("op,value,expected", [
    ("equals", "кружка", ["2"]),
    ("not_equals", "Кружка", ["1", "3", "4"]),
    ("contains", "ЧАЙНИК", ["1", "4"]),
    ("not_contains", "чайник", ["2", "3"]),
    ("gt", "1000", ["1", "4"]),
    ("lt", "1200,6", ["1", "2"]),
    ("empty", None, ["3"]),
    ("not_empty", None, ["1", "2", "4"]),
])
def test_filter_rows(app, op, value, expected):
    column = "price" if op in ("gt", "lt", "empty", "not_empty") else "name"
    values = {"table": PRICES, "column": column, "op": op}
    if value is not None:
        values["value"] = value
    rec, files, result, out = run(app, "std-filter-rows", **values)
    assert rec.status == "ok" and files == ["rows.csv"]
    rows = read_csv(out / "rows.csv")
    assert rows[0] == ["sku", "name", "price"] and [r[0] for r in rows[1:]] == expected
    assert result["main"] == "rows.csv"


def test_filter_case_sensitive(app):
    rec, files, _, out = run(app, "std-filter-rows", table=PRICES, column="name", op="contains",
                             value="Чайник", ignore_case=False)
    assert [r[0] for r in read_csv(out / "rows.csv")[1:]] == ["1"]


def test_filter_nothing_left_means_no_file(app):
    rec, files, result, _ = run(app, "std-filter-rows", table=PRICES, column="name", op="equals", value="вилка")
    assert rec.status == "ok" and files == []
    assert "файла нет" in result["summary"]
    assert repair.suspicion(app, rec) is None  # необязательный выход и без count: не тихая поломка


def test_filter_errors(app):
    rec, files, result, _ = run(app, "std-filter-rows", table=PRICES, column="цена", op="equals", value="1")
    assert rec.status == "failed" and "нет колонки «цена»" in result["summary"] and files == []
    rec, _, result, _ = run(app, "std-filter-rows", table=PRICES, column="price", op="gt", value="много")
    assert rec.status == "failed" and "должно быть числом" in result["summary"]
    rec, _, result, _ = run(app, "std-filter-rows", table=PRICES, column="price", op="equals")
    assert rec.status == "failed" and "нужно значение" in result["summary"]


@pytest.mark.parametrize("values,answer", [
    ({"check": "rows_gt", "rows": 3}, "yes"),
    ({"check": "rows_gt", "rows": 4}, "no"),
    ({"check": "any_row", "column": "price", "op": "gt", "value": "2000"}, "yes"),
    ({"check": "any_row", "column": "name", "op": "equals", "value": "вилка"}, "no"),
    ({"check": "all_rows", "column": "sku", "op": "not_empty"}, "yes"),
    ({"check": "all_rows", "column": "price", "op": "not_empty"}, "no"),
])
def test_if(app, values, answer):
    rec, files, result, out = run(app, "std-if", table=PRICES, **values)
    assert rec.status == "ok" and files == [f"{answer}.csv"]
    assert result["main"] == f"{answer}.csv" and result["summary"].startswith("да" if answer == "yes" else "нет")
    rows = read_csv(out / f"{answer}.csv")
    assert rows[0] == ["sku", "name", "price"] and len(rows) == 5  # таблица целиком
    assert repair.suspicion(app, rec) is None  # второй файл не появился, и это нормально


def test_if_without_table(app):
    rec, files, _, out = run(app, "std-if", check="rows_gt", rows=0)
    assert rec.status == "ok" and files == ["no.csv"] and (out / "no.csv").read_text(encoding="utf-8") == ""


def test_if_errors(app):
    rec, _, result, _ = run(app, "std-if", table=PRICES, check="any_row", op="equals", value="1")
    assert rec.status == "failed" and "нужна колонка" in result["summary"]
    rec, _, result, _ = run(app, "std-if", table=PRICES, check="any_row", column="нет", op="empty")
    assert rec.status == "failed" and "нет колонки «нет»" in result["summary"]


WB = table(("sku", "price"), ("1", "100"), ("2", "200"))
OZON = table(("sku", "price"), ("3", "300"))
OZON_EXTRA = table(("price", "sku", "stock"), ("400", "4", "7"))


def test_merge_plain(app):
    rec, files, result, out = run(app, "std-merge-tables", table_1=WB, table_2=OZON)
    assert rec.status == "ok" and files == ["merged.csv"] and result["main"] == "merged.csv"
    assert read_csv(out / "merged.csv") == [["sku", "price"], ["1", "100"], ["2", "200"], ["3", "300"]]
    assert "склеено таблиц: 2, строк: 3" == result["summary"]


def test_merge_with_source_column_and_gaps(app):
    rec, _, _, out = run(app, "std-merge-tables", table_1=WB, table_3=OZON_EXTRA, source_column="Откуда",
                         source_names=["WB", "", "Ozon"])
    assert rec.status == "ok"
    assert read_csv(out / "merged.csv") == [
        ["Откуда", "sku", "price", "stock"],
        ["WB", "1", "100", ""], ["WB", "2", "200", ""], ["Ozon", "4", "400", "7"],
    ]


def test_merge_column_modes(app):
    rec, _, _, out = run(app, "std-merge-tables", table_1=WB, table_2=OZON_EXTRA, columns="common")
    assert read_csv(out / "merged.csv") == [["sku", "price"], ["1", "100"], ["2", "200"], ["4", "400"]]
    rec, files, result, _ = run(app, "std-merge-tables", table_1=WB, table_2=OZON_EXTRA, columns="same")
    assert rec.status == "failed" and files == []
    assert result["summary"] == "колонки «Таблица 2» не совпадают с «Таблица 1»: лишние «stock»"
    rec, _, _, out = run(app, "std-merge-tables", table_1=WB, table_2=table(("price", "sku"), ("9", "9")), columns="same")
    assert rec.status == "ok" and read_csv(out / "merged.csv")[-1] == ["9", "9"]  # порядок колонок первой таблицы
    rec, _, result, _ = run(app, "std-merge-tables", table_1=WB, source_column="sku")
    assert rec.status == "failed" and "уже есть в таблице" in result["summary"]


def test_select_and_rename(app):
    rec, files, result, out = run(app, "std-select-columns", table=PRICES, columns=["price = Цена", "sku"])
    assert rec.status == "ok" and files == ["table.csv"] and result["main"] == "table.csv"
    rows = read_csv(out / "table.csv")
    assert rows[0] == ["Цена", "sku"] and rows[1] == ["1 200,50", "1"] and len(rows) == 5
    assert result["summary"] == "колонок: 2, переименовано: 1, строк: 4"


def test_select_missing_columns(app):
    rec, _, result, _ = run(app, "std-select-columns", table=PRICES, columns=["sku", "stock"])
    assert rec.status == "failed" and "нет колонок: «stock»" in result["summary"]
    rec, _, _, out = run(app, "std-select-columns", table=PRICES, columns=["sku", "stock"], missing="empty")
    assert read_csv(out / "table.csv")[:2] == [["sku", "stock"], ["1", ""]]
    rec, _, _, out = run(app, "std-select-columns", table=PRICES, columns=["sku", "stock"], missing="skip")
    assert read_csv(out / "table.csv")[0] == ["sku"]
    rec, _, result, _ = run(app, "std-select-columns", table=PRICES, columns=["sku = id", "name = id"])
    assert rec.status == "failed" and "повторяются колонки: «id»" in result["summary"]


def test_nodes_chain_through_files(app):
    """Выход одного узла это вход следующего: склеить → отфильтровать → если."""
    rec, _, _, out = run(app, "std-merge-tables", table_1=WB, table_2=OZON, source_column="Магазин",
                         source_names=["WB", "Ozon"])
    merged = str(out / "merged.csv")
    rec = launch.prepare(app, "std-filter-rows", sets=[f"table=@{merged}", "column=price", "op=gt", "value=150"])
    done = launch.supervise(app, rec.run.id, rec.payload())
    rows_csv = app.paths.run_dir(done.id) / "output" / "rows.csv"
    assert read_csv(rows_csv) == [["Магазин", "sku", "price"], ["WB", "2", "200"], ["Ozon", "3", "300"]]
    rec = launch.prepare(app, "std-if", sets=[f"table=@{rows_csv}", "check=rows_gt", "rows=1"])
    done = launch.supervise(app, rec.run.id, rec.payload())
    assert (app.paths.run_dir(done.id) / "output" / "yes.csv").is_file()
