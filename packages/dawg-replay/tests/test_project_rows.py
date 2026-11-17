"""Стрелка в любой вход и проход по строкам (dawg_replay.project_runs, спека m6-node-inputs)."""

import csv
import io
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from dawg_replay import cli, launch
from dawg_replay import project_runs as P
from dawg_replay import projects as PJ
from dawg_replay.app import App
from dawg_replay.errors import ReplayError
from project_run_scripts import CITIES, PER_CITY, TARGET


@pytest.fixture(autouse=True)
def script_command(monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))


def _inline(home, kr, fn, *args):
    def work():
        with App(home, keyring_backend=kr) as a:
            fn(a, *args)

    t = threading.Thread(target=work)
    t.start()
    t.join()


@pytest.fixture
def steps(home, kr, monkeypatch):
    """Шаг и строка идут в своём потоке со своим App, как в своём процессе."""
    started = []

    def spawn(app, prun_id, node):
        started.append(node)
        _inline(home, kr, P.run_step, prun_id, node)

    def spawn_row(app, prun_id, node, idx):
        started.append((node, idx))
        _inline(home, kr, P.run_row, prun_id, node, idx)

    monkeypatch.setattr(P, "_spawn_step", spawn)
    monkeypatch.setattr(P, "_spawn_row", spawn_row)
    return started


@pytest.fixture
def scripts(app, tmp_path):
    src = tmp_path / "scripts"
    src.mkdir()
    for fname, text in (("cities.py", CITIES.format(name="cities", title="Список городов")),
                        ("wb.py", PER_CITY.format(name="wb", title="WB по городу")),
                        ("gs.py", TARGET.format(name="gs", title="Google"))):
        (src / fname).write_text(text, encoding="utf-8")
        app.registry.add(src / fname)
    return app


def project(app, tmp_path, text, name="rows"):
    f = tmp_path / f"{name}.project.toml"
    f.write_text(text, encoding="utf-8")
    if app.projects.list() and any(i.name == name for i in app.projects.list()):
        app.projects.update(f)
    else:
        app.projects.add(f)
    return name


ROWS = '''\
name = "rows"
title = "Цены по городам"

[[nodes]]
id = "cities"
script = "cities"
{cities}

[[nodes]]
id = "wb"
script = "wb"
{wb}

[[nodes]]
id = "gs"
script = "gs"

[[edges]]
from = "cities"
to = "wb"
map = [{{ from = "cities.csv", column = "Город", to = "city", rows = "each" }}]
{edge}

[[edges]]
from = "wb"
to = "gs"
when = "any"
map = [{{ from = "prices.csv", to = "cards" }}]
'''


def rows_project(app, tmp_path, cities="", wb="", edge=""):
    return project(app, tmp_path, ROWS.format(cities=cities, wb=wb, edge=edge))


def table(path):
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def out(app, run_id, name):
    return app.paths.run_dir(run_id) / "output" / name


def test_each_runs_once_per_row_and_merges(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, rows_project(app, tmp_path))
    assert pr.status == "ok", P.get(app, pr.id).to_dict()
    wb = pr.step("wb")
    assert wb.status == "ok" and wb.each and wb.rows_total == 3
    assert [(r.index, r.label, r.status, r.values) for r in wb.rows] == [
        (0, "Москва", "ok", {"city": "Москва"}), (1, "Казань", "ok", {"city": "Казань"}),
        (2, "Тверь", "ok", {"city": "Тверь"})]
    for r in wb.rows:
        rec = app.runs.get(r.run_id)
        assert (rec.project_run, rec.project_node, rec.project_row, rec.params["city"]) == (pr.id, "wb", r.index, r.label)
    merged = app.runs.get(wb.run_id)
    assert (merged.status, merged.project_row, merged.trigger) == ("ok", None, "project")
    assert table(out(app, wb.run_id, "prices.csv")) == [
        {"Город": "Москва", "sku": "A-Москва", "price": "10"}, {"Город": "Казань", "sku": "A-Казань", "price": "10"},
        {"Город": "Тверь", "sku": "A-Тверь", "price": "10"}]
    assert sorted(p.name for p in out(app, wb.run_id, "").iterdir()) == [
        "prices.csv", "seen (Казань).json", "seen (Москва).json", "seen (Тверь).json"]
    got = json.loads(out(app, pr.step("gs").run_id, "got.json").read_text(encoding="utf-8"))
    assert [r["sku"] for r in got["rows"]] == ["A-Москва", "A-Казань", "A-Тверь"]  # дальше как результат одного шага
    assert wb.to_dict()["rows_summary"]["text"] == "3 из 3"


def test_where_and_limit_choose_rows(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, rows_project(app, tmp_path, edge='where = { column = "Активен", op = "eq", value = "ДА" }'))
    assert [r.label for r in pr.step("wb").rows] == ["Москва", "Тверь"]  # без учёта регистра, как у фильтра
    pr = P.start(app, rows_project(app, tmp_path, edge='where = { column = "Страниц", op = "gt", value = "1" }\nlimit = 1'))
    assert [r.label for r in pr.step("wb").rows] == ["Москва"]


def test_empty_table_skips_step_with_no_rows(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, rows_project(app, tmp_path, edge='where = { column = "Город", op = "eq", value = "Омск" }'))
    wb = pr.step("wb")
    assert (wb.status, wb.reason, wb.rows_total) == ("skipped", "нет строк", 0)
    assert pr.step("gs").status == "skipped" and pr.status == "partial"


def test_continue_after_failed_row_gives_partial_and_retry_repeats_only_failed(scripts, tmp_path, steps, capsys):
    app = scripts
    name = rows_project(app, tmp_path, wb='values = { fail = "Казань" }')
    pr = P.start(app, name)
    wb = pr.step("wb")
    assert wb.status == "partial" and pr.status == "partial"
    assert [r.status for r in wb.rows] == ["ok", "failed", "ok"]
    assert wb.reason == "строк 2 из 3, упало 1"
    assert [r["Город"] for r in table(out(app, wb.run_id, "prices.csv"))] == ["Москва", "Тверь"]
    assert app.runs.get(wb.run_id).status == "partial"
    text = cli_text(capsys, ["project", "status", pr.id])
    assert "WB по городу: частично, строк 2 из 3, упало 1" in text
    assert "строка 2 «Казань»: упал" in text

    steps.clear()
    again = P.retry(app, pr.id)
    rows = again.step("wb").rows
    assert [(r.label, r.reused) for r in rows] == [("Москва", True), ("Казань", False), ("Тверь", True)]
    assert [r.run_id for r in rows if r.reused] == [wb.rows[0].run_id, wb.rows[2].run_id]
    assert ("wb", 1) in steps and ("wb", 0) not in steps and ("wb", 2) not in steps
    assert rows[1].status == "failed"  # скрипт падает на Казани снова


def test_stop_on_error_skips_remaining_rows(scripts, tmp_path, steps):
    app = scripts
    pr = P.start(app, rows_project(app, tmp_path, wb='values = { fail = "Москва" }', edge='on_error = "stop"'))
    wb = pr.step("wb")
    assert wb.status == "failed" and [r.status for r in wb.rows] == ["failed", "skipped", "skipped"]
    assert wb.rows[1].reason == "проход остановлен: упала строка «Москва»"
    assert wb.run_id is None  # ни одна строка не дошла: сборки нет
    assert pr.step("gs").status == "not_started" and "нет запуска" in pr.step("gs").reason


def test_all_rows_failed_gives_failed(scripts, tmp_path, steps):
    app = scripts
    text = ROWS.format(cities='values = { csv = "Город\\\\nКазань" }', wb='values = { fail = "Казань" }', edge="")
    pr = P.start(app, project(app, tmp_path, text))
    assert pr.step("wb").status == "failed" and pr.status == "failed"


def test_rows_wait_for_parallel_and_app_limit(scripts, tmp_path, monkeypatch, home, kr):
    app = scripts
    started = []

    def spawn(app_, prun_id, node):
        _inline(home, kr, P.run_step, prun_id, node)

    monkeypatch.setattr(P, "_spawn_step", spawn)
    monkeypatch.setattr(P, "_spawn_row", lambda a, prun_id, node, idx: started.append(idx))
    pr = P.start(app, rows_project(app, tmp_path, edge="parallel = 2"))
    assert started == [0, 1]
    assert [r.status for r in pr.step("wb").rows] == ["starting", "starting", "pending"]
    P.stop(app, pr.id)

    started.clear()
    _set_limit(app, 1)
    pr = P.start(app, rows_project(app, tmp_path, edge="parallel = 2"))
    assert started == [0]
    P.stop(app, pr.id)
    pr = P.get(app, pr.id)
    assert [r.status for r in pr.step("wb").rows] == ["cancelled", "skipped", "skipped"]
    assert pr.status == "cancelled"


def _set_limit(app, n):
    app.set_config("max_parallel_runs", str(n))


def test_column_first_all_and_secret_by_arrow(scripts, tmp_path, steps, monkeypatch):
    app = scripts
    app.secrets.set_secret("wb", "token", "из-хранилища")
    text = '''\
name = "rows"
title = "Колонки"

[[nodes]]
id = "cities"
script = "cities"

[[nodes]]
id = "wb"
script = "wb"

[[edges]]
from = "cities"
to = "wb"
map = [
  { from = "cities.csv", column = "Город", to = "city" },
  { from = "cities.csv", column = "Страниц", to = "pages" },
  { from = "cities.csv", column = "Город", to = "tags", rows = "all" },
  { from = "cities.csv", column = "Активен", to = "token" },
]
where = { column = "Активен", op = "eq", value = "да" }
'''
    pr = P.start(app, project(app, tmp_path, text))
    assert pr.status == "ok", P.get(app, pr.id).to_dict()
    run_id = pr.step("wb").run_id
    seen = json.loads(out(app, run_id, "seen.json").read_text(encoding="utf-8"))
    assert seen == {"city": "Москва", "pages": 2, "tags": ["Москва", "Тверь"], "token": "да"}  # стрелка важнее хранилища
    rec = app.runs.get(run_id)
    assert "token" not in rec.params and rec.params["pages"] == 2
    assert all("да" not in json.dumps(e, ensure_ascii=False) for e in rec.inputs if e.get("param") == "token")


def test_cell_not_matching_type_does_not_start_row(scripts, tmp_path, steps):
    app = scripts
    text = ROWS.replace('map = [{{ from = "cities.csv", column = "Город", to = "city", rows = "each" }}]',
                        'map = [{{ from = "cities.csv", column = "Город", to = "city", rows = "each" }}, '
                        '{{ from = "cities.csv", column = "Страниц", to = "pages", rows = "each" }}]')
    pr = P.start(app, project(app, tmp_path, text.format(
        cities='values = { csv = "Город,Страниц\\\\nМосква,2\\\\nКазань,много" }', wb="", edge="")))
    rows = pr.step("wb").rows
    assert [(r.label, r.status) for r in rows] == [("Москва, 2", "ok"), ("Казань, много", "not_started")]
    assert "нужно число" in rows[1].reason


def test_value_by_arrow(scripts, tmp_path, steps, monkeypatch):
    app = scripts
    monkeypatch.setattr(PJ, "value_outputs", lambda m: (
        SimpleNamespace(name="city", type="string", secret=False, kind="value"),
        SimpleNamespace(name="pages", type="string", secret=False, kind="value"),
        SimpleNamespace(name="token", type="secret", secret=True, kind="value")) if m.name == "cities" else ())
    given = {"city": "Самара", "pages": "4", "token": "т-1"}
    monkeypatch.setattr(P, "_read_values", lambda app_, run_id: given if app_.runs.get(run_id).script_name == "cities" else {})
    text = '''\
name = "rows"
title = "Значения"

[[nodes]]
id = "cities"
script = "cities"

[[nodes]]
id = "wb"
script = "wb"

[[edges]]
from = "cities"
to = "wb"
map = [{ value = "city", to = "city" }, { value = "pages", to = "pages" }, { value = "token", to = "token" }]
'''
    problems = app.projects.check(PJ.parse(text))
    assert [p.text for p in problems if p.level == "warning"] == [
        "стрелка cities → wb: значение `pages` (string) во вход «Страниц» (int): подойдёт ли, видно при старте шага"]
    pr = P.start(app, project(app, tmp_path, text))
    seen = json.loads(out(app, pr.step("wb").run_id, "seen.json").read_text(encoding="utf-8"))
    assert (seen["city"], seen["pages"], seen["token"]) == ("Самара", 4, "т-1")
    given["pages"] = "четыре"
    pr = P.start(app, "rows")
    assert pr.step("wb").status == "not_started" and "нужно число" in pr.step("wb").reason
    given.pop("city")  # значение не отдано: обязательный вход пуст
    given["pages"] = "4"
    pr = P.start(app, "rows")
    assert pr.step("wb").status == "not_started" and "city" in pr.step("wb").reason


def test_values_collected_into_values_csv(scripts, tmp_path, steps, monkeypatch):
    app = scripts
    monkeypatch.setattr(P, "_shown_values", lambda app_, run_id: [
        {"name": "price", "title": "Цена", "type": "int", "secret": False, "value": 7},
        {"name": "token", "title": "Токен", "type": "secret", "secret": True, "value": None}])
    pr = P.start(app, rows_project(app, tmp_path))
    assert table(out(app, pr.step("wb").run_id, "values.csv")) == [
        {"Город": "Москва", "price": "7"}, {"Город": "Казань", "price": "7"}, {"Город": "Тверь", "price": "7"}]


def cli_text(capsys, argv):
    capsys.readouterr()
    code = cli.main(argv)
    text = capsys.readouterr().out
    assert code == 0, text
    return text


# --- файл проекта и проверка ---------------------------------------------------------------


def test_old_files_read_and_map_written(scripts):
    pr = PJ.parse(ROWS.format(cities="", wb="", edge='limit = 5\nparallel = 3\non_error = "stop"')
                  .replace('map = [{ from = "prices.csv", to = "cards" }]', 'files = [{ from = "prices.csv", to = "cards" }]'))
    e1, e2 = pr.edges
    assert [(m.kind, m.file, m.column, m.rows) for m in e1.maps] == [("column", "cities.csv", "Город", "each")]
    assert (e1.limit, e1.parallel, e1.on_error) == (5, 3, "stop")
    assert e2.files == e2.maps and e2.maps[0].kind == "file"
    text = PJ.dump(pr)
    assert 'map = [{ from = "prices.csv", to = "cards" }]' in text and "files" not in text
    assert 'map = [{ from = "cities.csv", column = "Город", rows = "each", to = "city" }]' in text
    assert PJ.parse(text) == pr
    assert PJ.from_dict(pr.to_dict()) == pr
    assert PJ.from_dict(json.loads(json.dumps(pr.to_dict()))) == pr


@pytest.mark.parametrize("edge, part", [
    ('map = [{ from = "a.csv", value = "x", to = "city" }]', "и from, и value"),
    ('map = [{ value = "x", column = "c", to = "city" }]', "column и rows бывают только у файла"),
    ('map = [{ from = "a.csv", rows = "each", to = "city" }]', "rows бывает только с column"),
    ('map = [{ from = "a.csv", column = "c", rows = "many", to = "city" }]', "rows `many`"),
    ('map = [{ from = "a.csv", to = "cards" }]\nlimit = 2', "limit и where выбирают строки"),
    ('map = [{ from = "a.csv", column = "c", to = "city" }]\nparallel = 2', "parallel и on_error задают проход"),
    ('map = [{ from = "a.csv", column = "c", to = "city", rows = "each" }]\nparallel = 0', "parallel это целое"),
    ('map = [{ from = "a.csv", column = "c", to = "city", rows = "each" }]\non_error = "retry"', "on_error `retry`"),
    ('map = [{ from = "a.csv", column = "c", to = "city" }]\nwhere = { column = "c", op = "like", value = "x" }',
     "where op `like`"),
    ('map = [{ from = "a.csv", column = "c", to = "city" }]\nwhere = { column = "c", op = "gt", value = "x" }',
     "value это число"),
    ('map = [{ from = "a.csv", to = "x" }]\nfiles = [{ from = "a.csv", to = "x" }]', "map и files сразу"),
])
def test_map_structure_errors(edge, part):
    text = f'name = "x"\n[[nodes]]\nid = "a"\nscript = "a"\n[[nodes]]\nid = "b"\nscript = "b"\n' \
           f'[[edges]]\nfrom = "a"\nto = "b"\n{edge}\n'
    with pytest.raises(ReplayError) as e:
        PJ.parse(text)
    assert part in e.value.message


def test_check_map_kinds(scripts):
    app = scripts
    text = '''\
name = "x"

[[nodes]]
id = "cities"
script = "cities"

[[nodes]]
id = "other"
script = "cities"

[[nodes]]
id = "wb"
script = "wb"

[[nodes]]
id = "gs"
script = "gs"

[[edges]]
from = "cities"
to = "wb"
map = [{ from = "cities.csv", column = "Город", to = "city", rows = "each" },
       { from = "cities.csv", column = "Город", to = "fail", rows = "all" },
       { from = "cities.csv", column = "Нет", to = "pages" },
       { value = "token", to = "token" }]

[[edges]]
from = "other"
to = "wb"
map = [{ from = "cities.csv", column = "Город", to = "tags", rows = "each" }]

[[edges]]
from = "wb"
to = "gs"
map = [{ from = "prices.csv", column = "sku", to = "cards" }, { value = "x", to = "sheet" }]
'''
    problems = app.projects.check(PJ.parse(text))
    errs = "\n".join(p.text for p in problems if p.level == "error")
    for part in ["rows = \"all\" (вся колонка списком) только для входов-списков",
                 "у скрипта `cities` нет выходного значения `token`",
                 "узел `wb`: проход по строкам идёт по одной таблице одного узла",
                 "вход `cards` типа table: колонка идёт во вход-значение",
                 "у узла `wb` проход по строкам, его значения собраны в таблицу values.csv"]:
        assert part in errs, part
    warns = "\n".join(p.text for p in problems if p.level == "warning")
    assert "не объявлена колонка «Нет»" in warns
    assert [p for p in problems if p.level == "blocker"] == []  # входы со стрелкой не пустые


def test_blocker_saves_but_does_not_run(scripts, tmp_path, steps):
    app = scripts
    text = 'name = "b"\n[[nodes]]\nid = "wb"\nscript = "wb"\n'
    f = tmp_path / "b.project.toml"
    f.write_text(text, encoding="utf-8")
    r = app.projects.add(f)
    assert [(w.level, w.text, w.param) for w in r.warnings] == [("blocker", "узел `wb`: не задан «Город»", "city")]
    with pytest.raises(ReplayError) as e:
        P.start(app, "b")
    assert e.value.code == "project_invalid" and "не задан «Город»" in e.value.message
    assert steps == []
