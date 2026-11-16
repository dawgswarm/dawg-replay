"""Проекты (dawg_replay.projects): разбор файла, проверка, версии, экспорт и импорт, CLI project."""

from dataclasses import replace

import pytest
from dawg_replay import projects as P
from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from project_scripts import SOURCE, TARGET

BASIC = '''\
# схема агента: комментарий должен сохраниться в версии
name = "wb-to-google"
title = "Карточки WB в Google"
description = "Два магазина, общая таблица"
timeout_minutes = 240

[[nodes]]
id = "wb1"
script = "wb"
preset = "Магазин 1"
x = 40
y = 60

[[nodes]]
id = "wb2"
script = "wb"
preset = "Магазин 2"

[[nodes]]
id = "google"
script = "gs"
preset = "Общая"
wait = "any"

[[edges]]
from = "wb1"
to = "google"
files = [{ from = "cards.csv", to = "cards" }]
source_column = "Магазин"

[[edges]]
from = "wb2"
to = "google"
when = "ok"
if_file = "cards.csv"
files = [{ from = "cards.csv", to = "cards" }, { from = "report.pdf", to = "docs" }]
'''


def register(app, tmp_path, text, filename):
    f = tmp_path / filename
    f.write_text(text, encoding="utf-8")
    app.registry.add(f)


def setup_scripts(app, tmp_path, columns='"sku", "name"'):
    register(app, tmp_path, SOURCE.format(name="wb", title="WB: карточки", columns=columns), "wb.py")
    register(app, tmp_path, SOURCE.format(name="oz", title="Ozon: карточки", columns='"sku", "price"'), "oz.py")
    register(app, tmp_path, TARGET.format(name="gs"), "gs.py")


@pytest.fixture
def scripts(app, tmp_path):
    setup_scripts(app, tmp_path)
    app.save_preset("wb", "Магазин 1", {"shop": "один", "pages": 3})
    app.save_preset("wb", "Магазин 2", {"shop": "два"})
    app.save_preset("gs", "Общая", {"sheet": "Лист1", "keys": [{"_row": 2, "k": "1"}]}, {"keys": "keys.csv"})
    return app


def write(tmp_path, text, name="p.project.toml"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def errors(problems):
    return [p.text for p in problems if p.level == "error"]


def warnings(problems):
    return [p.text for p in problems if p.level == "warning"]


def parse_error(text):
    with pytest.raises(ReplayError) as e:
        P.parse(text, "t.project.toml")
    assert e.value.code == "invalid_project"
    return [d["text"] for d in e.value.details]


# --- разбор ---


def test_parse_defaults_and_order():
    pr = P.parse(BASIC)
    assert pr.name == "wb-to-google" and pr.timeout_minutes == 240
    assert [n.id for n in pr.nodes] == ["wb1", "wb2", "google"]
    wb1 = pr.node("wb1")
    assert (wb1.x, wb1.y, wb1.wait, wb1.values, wb1.title) == (40, 60, "all", {}, None)
    assert pr.node("google").wait == "any"
    e1, e2 = pr.edges
    assert (e1.when, e1.if_file, e1.source_column) == ("partial", None, "Магазин")
    assert (e2.when, e2.if_file) == ("ok", "cards.csv")
    assert [f.to_dict() for f in e2.files] == [{"file": "cards.csv", "param": "cards"},
                                               {"file": "report.pdf", "param": "docs"}]
    assert pr.incoming("google") == [e1, e2] and pr.outgoing("wb1") == [e1]
    assert pr.order() == ["wb1", "wb2", "google"]
    with pytest.raises(ReplayError) as e:
        pr.node("nope")
    assert e.value.code == "node_not_found"


def test_order_puts_sources_first():
    pr = P.parse('name = "x"\n[[nodes]]\nid = "c"\nscript = "s"\n[[nodes]]\nid = "a"\nscript = "s"\n'
                 '[[nodes]]\nid = "b"\nscript = "s"\n'
                 '[[edges]]\nfrom = "a"\nto = "c"\n[[edges]]\nfrom = "b"\nto = "a"\n')
    assert pr.order() == ["b", "a", "c"]


def test_parse_structure_errors():
    texts = parse_error('''\
name = "Bad Name"
extra = 1
timeout_minutes = 0

[[nodes]]
id = "a"
script = "wb"
wait = "some"
colour = "red"

[[nodes]]
id = "a"
script = "wb"

[[nodes]]
id = "b"
preset_values = { shop = "x" }
script = "wb"

[[nodes]]
script = "wb"

[[edges]]
from = "a"
to = "zzz"

[[edges]]
from = "a"
to = "a"

[[edges]]
from = "a"
to = "b"
when = "always"
if_file = "/etc/passwd"
files = [{ from = "../x.csv", to = "cards" }, { from = "a.csv", to = "p" }, { from = "b.csv", to = "p" }]

[[edges]]
from = "a"
to = "b"
''')
    joined = "\n".join(texts)
    for part in ["имя проекта `Bad Name`", "неизвестное поле `extra`", "timeout_minutes", "wait `some`",
                 "неизвестное поле `colour`", "id повторяется", "preset_values без preset", "узел 4: нет id",
                 "узла `zzz` нет", "из узла в него же", "when `always`", "if_file", "выходить из каталога",
                 "в параметр `p` этой стрелки уже идёт файл", "такая стрелка уже есть"]:
        assert part in joined, part


def test_parse_toml_error_and_empty():
    assert "ошибка TOML" in parse_error('name = "x"\n[[nodes]\n')[0]
    assert any("нет узлов" in t for t in parse_error('name = "x"\n'))
    assert any("нет имени" in t for t in parse_error('[[nodes]]\nid = "a"\nscript = "s"\n'))


def test_dump_roundtrip():
    pr = P.parse(BASIC)
    pr = replace(pr, nodes=(replace(pr.nodes[0], values={"pages": 2, "shop": 'ма"г\\'}, title="Первый"),
                            *pr.nodes[1:]))
    text = P.dump(pr)
    assert P.parse(text) == pr
    with_values = P.dump(pr, {"wb1": {"shop": "один", "rows": [{"a": "1", "b c": "2"}], "none": None}})
    again = P.parse(with_values)
    assert again.node("wb1").preset_values == {"shop": "один", "rows": [{"a": "1", "b c": "2"}]}
    assert replace(again, nodes=tuple(replace(n, preset_values=None) for n in again.nodes)) == pr


# --- проверка ---


def test_check_clean_and_warnings(scripts, tmp_path):
    app = scripts
    problems = app.projects.check(P.parse(BASIC))
    assert errors(problems) == []
    assert warnings(problems) == []


def test_check_errors(scripts):
    app = scripts
    pr = P.parse('''\
name = "bad"

[[nodes]]
id = "a"
script = "wb"
preset = "Нет такого"
values = { pages = "много", nope = 1 }

[[nodes]]
id = "b"
script = "missing"

[[nodes]]
id = "c"
script = "gs"
values = { token = "x" }

[[nodes]]
id = "d"
script = "wb"

[[edges]]
from = "a"
to = "c"
files = [{ from = "cards.csv", to = "sheet" }, { from = "x.csv", to = "keys" }, { from = "x", to = "nope" }, { from = "report.pdf", to = "doc" }]

[[edges]]
from = "d"
to = "c"
files = [{ from = "report.pdf", to = "doc" }]

[[edges]]
from = "c"
to = "d"
''')
    errs = "\n".join(errors(app.projects.check(pr)))
    for part in ["нет пресета «Нет такого»", "`pages`", "`nope`: такого параметра нет", "скрипт `missing` не зарегистрирован",
                 "`token`: секрет здесь не хранится", "параметр `sheet` типа string", "параметр `keys` секретный",
                 "нет параметра `nope`", "в параметр `doc` (один файл) идут несколько стрелок",
                 "круг: c → d → c"]:
        assert part in errs, part


def test_check_warnings(scripts, tmp_path):
    app = scripts
    pr = P.parse('''\
name = "warn"

[[nodes]]
id = "wb"
script = "wb"

[[nodes]]
id = "oz"
script = "oz"

[[nodes]]
id = "gs"
script = "gs"

[[edges]]
from = "wb"
to = "gs"
if_file = "nope.csv"
files = [{ from = "cards.csv", to = "cards" }]

[[edges]]
from = "oz"
to = "gs"
files = [{ from = "cards.csv", to = "cards" }, { from = "other.csv", to = "docs" }]
source_column = "Откуда"

[[edges]]
from = "wb"
to = "oz"
files = []
source_column = "Откуда"
''')
    problems = app.projects.check(pr)
    assert errors(problems) == []
    w = "\n".join(warnings(problems))
    for part in ["if_file `nope.csv` не объявлен", "в `cards.csv` нет колонки `name`", "файл `other.csv` не объявлен",
                 "разными колонками", "source_column действует только на таблицы",
                 "узел `gs`: нет значения «Ключи», «Лист»"]:
        assert part in w, part
    assert "узел `gs`" in w and not any("«Карточки»" in x for x in warnings(problems))


def test_if_file_on_required_output_warns_when_kit_knows_optional(scripts, monkeypatch):
    app = scripts
    pr = P.parse('name = "x"\n[[nodes]]\nid = "a"\nscript = "wb"\n[[nodes]]\nid = "b"\nscript = "oz"\n'
                 '[[edges]]\nfrom = "a"\nto = "b"\nif_file = "cards.csv"\n')
    assert warnings(app.projects.check(pr)) == []  # без поля optional в kit предупреждения нет
    out = app.registry.manifest("wb").outputs[0]
    monkeypatch.setattr(P, "output_for", lambda m, spec: (True, type("O", (), {"name": out.name, "optional": False,
                                                                             "columns": out.columns})()))
    assert any("будет активна всегда" in t for t in warnings(app.projects.check(pr)))


# --- хранение и версии ---


def test_add_update_versions_rollback_remove(scripts, tmp_path):
    app = scripts
    f = write(tmp_path, BASIC)
    r = app.projects.add(f)
    assert (r.status, r.version.version, r.previous_version, r.version.origin) == ("added", 1, None, "file")
    assert r.info.title == "Карточки WB в Google" and r.info.description == "Два магазина, общая таблица"
    stored = app.projects.load("wb-to-google")
    assert stored.text == BASIC  # с комментарием агента
    assert stored.project == P.parse(BASIC)

    assert app.projects.add(f).status == "unchanged"
    f.write_bytes(BASIC.replace("\n", "\r\n").encode("utf-8"))  # тот же файл с переводами строк Windows
    assert app.projects.add(f).status == "unchanged"
    assert app.projects.update(f).status == "unchanged"
    f2 = write(tmp_path, BASIC.replace("x = 40", "x = 80"))
    with pytest.raises(ReplayError) as e:
        app.projects.add(f2)
    assert e.value.code == "project_exists"
    r2 = app.projects.update(f2)
    assert (r2.status, r2.version.version, r2.previous_version) == ("added", 2, 1)
    assert [(v.version, v.current) for v in app.projects.versions("wb-to-google")] == [(1, False), (2, True)]
    assert app.projects.load("wb-to-google", 1).project.node("wb1").x == 40

    v = app.projects.rollback("wb-to-google", 1)
    assert (v.version, v.current) == (1, True)
    assert app.projects.get("wb-to-google").current_version == 1
    r3 = app.projects.update(f2)  # тот же текст, что v2, но текущая v1: новая версия
    assert (r3.status, r3.version.version) == ("added", 3)
    with pytest.raises(ReplayError) as e:
        app.projects.rollback("wb-to-google", 9)
    assert e.value.code == "version_not_found"

    assert [x.name for x in app.projects.list()] == ["wb-to-google"]
    assert app.projects.using("wb") == ["wb-to-google"] and app.projects.using("oz") == []
    assert [x.version for x in app.projects.remove("wb-to-google")] == [1, 2, 3]
    assert app.projects.list() == []
    assert app.db.execute("SELECT COUNT(*) FROM project_versions").fetchone()[0] == 0
    with pytest.raises(ReplayError) as e:
        app.projects.get("wb-to-google")
    assert e.value.code == "project_not_found"


def test_update_unknown_and_errors_block_save(scripts, tmp_path):
    app = scripts
    with pytest.raises(ReplayError) as e:
        app.projects.update(write(tmp_path, BASIC))
    assert e.value.code == "project_not_found"
    with pytest.raises(ReplayError) as e:
        app.projects.add(write(tmp_path, BASIC.replace('script = "gs"', 'script = "nope"')))
    assert e.value.code == "invalid_project" and "не зарегистрирован" in e.value.message
    assert app.projects.list() == []
    with pytest.raises(ReplayError) as e:
        app.projects.add(tmp_path / "missing.project.toml")
    assert e.value.code == "file_not_found"


def test_save_from_editor(scripts, tmp_path):
    app = scripts
    app.projects.add(write(tmp_path, BASIC))
    stored = app.projects.load("wb-to-google")
    moved = replace(stored.project, nodes=(replace(stored.project.nodes[0], x=10.5, y=20.0), *stored.project.nodes[1:]))
    r = app.projects.save(moved, base_version=1)
    assert (r.status, r.version.version, r.version.origin) == ("added", 2, "editor")
    assert app.projects.load("wb-to-google").project == moved
    with pytest.raises(ReplayError) as e:
        app.projects.save(moved, base_version=1)  # холст отстал: агент или другая вкладка уже сохранили v2
    assert e.value.code == "project_changed" and e.value.details == {"current_version": 2}
    assert app.projects.save(moved, base_version=2).status == "unchanged"
    with pytest.raises(ReplayError) as e:
        app.projects.save(replace(moved, name="other"))
    assert e.value.code == "project_not_found"
    assert app.projects.save(replace(moved, name="other"), create=True).version.version == 1
    bad = replace(moved, edges=(*moved.edges, P.Edge("google", "wb1")))
    with pytest.raises(ReplayError) as e:
        app.projects.save(bad, base_version=2)
    assert e.value.code == "invalid_project" and "круг" in e.value.message


def test_node_title(scripts):
    app = scripts
    pr = P.parse(BASIC)
    assert app.projects.node_title(pr.node("wb1")) == "WB: карточки: Магазин 1"
    assert app.projects.node_title(P.Node("n", "missing")) == "missing"
    assert app.projects.node_title(P.Node("n", "wb", title="Свой")) == "Свой"


# --- экспорт и импорт ---


def test_export_and_import_on_other_machine(scripts, tmp_path, kr):
    app = scripts
    app.projects.add(write(tmp_path, BASIC))
    assert app.projects.export("wb-to-google", preset_values=False).text == BASIC
    ex = app.projects.export("wb-to-google")
    assert any("секретные таблицы пресета «Общая» не переносятся: «Ключи»" in w for w in ex.warnings)
    pr = P.parse(ex.text)
    assert pr.node("wb1").preset_values == {"shop": "один", "pages": 3}
    assert pr.node("google").preset_values == {"sheet": "Лист1"}

    other = tmp_path / "other"
    other.mkdir()
    with App(other / "home", keyring_backend=kr) as app2:
        setup_scripts(app2, other)
        app2.save_preset("wb", "Магазин 2", {"shop": "свой"})  # уже есть: не перезаписывается
        r = app2.projects.add(write(other, ex.text))
        assert r.status == "added"
        assert sorted(x["preset"] for x in r.presets_created) == ["Магазин 1", "Общая"]
        assert [x["preset"] for x in r.presets_kept] == ["Магазин 2"]
        assert app2.get_preset("wb", "Магазин 1").params == {"shop": "один", "pages": 3}
        assert app2.get_preset("wb", "Магазин 2").params == {"shop": "свой"}
        w = "\n".join(p.text for p in r.warnings)
        assert "создан без секретных значений «Ключи», заполните пресет" in w
        assert "не заданы секреты `token`" in w
        stored = app2.projects.load("wb-to-google")
        assert "preset_values" not in stored.text  # в версию значения пресетов не попадают
        assert stored.project == P.parse(BASIC)


def test_import_bad_preset_values_saves_nothing(scripts, tmp_path):
    app = scripts
    text = BASIC.replace('preset = "Магазин 1"', 'preset = "Новый"\npreset_values = { pages = "много" }')
    with pytest.raises(ReplayError) as e:
        app.projects.add(write(tmp_path, text))
    assert "preset_values" in e.value.message
    assert [p.name for p in app.list_presets("wb")] == ["Магазин 1", "Магазин 2"]
    assert app.projects.list() == []


def test_remove_script_reports_projects(scripts, tmp_path):
    app = scripts
    app.projects.add(write(tmp_path, BASIC))
    app.set_secret("gs", "token", "x")
    assert app.remove("gs")["in_projects"] == ["wb-to-google"]


# --- CLI ---


def test_cli_project(scripts, tmp_path, cli):
    f = write(tmp_path, BASIC)
    code, out = cli("project", "check", str(f))
    assert code == 0 and out["problems"] == [] and out["name"] == "wb-to-google"
    code, out = cli("project", "add", str(f))
    assert code == 0 and out["status"] == "added" and out["version"]["version"] == 1
    code, out = cli("project", "update", str(write(tmp_path, BASIC.replace("x = 40", "x = 1"))))
    assert code == 0 and out["version"]["version"] == 2
    code, out = cli("project", "list")
    assert [p["name"] for p in out["projects"]] == ["wb-to-google"]
    code, out = cli("project", "show", "wb-to-google", "--version", "1")
    assert code == 0 and out["version"]["version"] == 1 and out["content"]["nodes"][0]["x"] == 40
    assert out["problems"] == []
    code, out = cli("project", "check", "wb-to-google")
    assert code == 0
    code, out = cli("project", "versions", "wb-to-google")
    assert [v["version"] for v in out["versions"]] == [1, 2]
    code, out = cli("project", "rollback", "wb-to-google", "1")
    assert code == 0 and out["version"]["current"]
    dest = tmp_path / "out.project.toml"
    code, out = cli("project", "export", "wb-to-google", "-o", str(dest))
    assert code == 0 and out["file"] == str(dest) and "[nodes.preset_values]" in dest.read_text(encoding="utf-8")
    code, out = cli("project", "export", "wb-to-google", "--no-preset-values")
    assert out["text"] == BASIC
    code, out = cli("project", "remove", "wb-to-google")
    assert code == 1 and out["error"]["code"] == "confirmation_required"
    code, out = cli("project", "remove", "wb-to-google", "--yes")
    assert code == 0 and out["versions"] == [1, 2]
    code, out = cli("project", "check", str(write(tmp_path, BASIC.replace('script = "gs"', 'script = "zz"'))))
    assert code == 1 and out["error"]["code"] == "invalid_project"
    code, out = cli("project", "check", "nothing")
    assert code == 1 and out["error"]["code"] == "project_not_found"


def test_cli_project_text(scripts, tmp_path, capsys):
    from dawg_replay.cli import main

    f = write(tmp_path, BASIC)
    assert main(["project", "add", str(f)]) == 0
    assert "проект добавлен: wb-to-google v1" in capsys.readouterr().out
    assert main(["project", "show", "wb-to-google"]) == 0
    text = capsys.readouterr().out
    assert "google  Выгрузка в Google: Общая  (gs, пресет «Общая»)  [после любой входящей]" in text
    assert "wb2 → google; только готово; если есть report.pdf" not in text
    assert "wb2 → google; только готово; если есть cards.csv; cards.csv → cards, report.pdf → docs" in text
