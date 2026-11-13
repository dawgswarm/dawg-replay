"""Форма параметров по манифесту (dawg_replay.forms)."""

import json
import sys

import pytest
from dawg_replay_kit.manifest import parse_manifest

from dawg_replay import forms, launch

ALL_TYPES = """\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2"]
# ///

# /// dawg-script
# contract = 1
# name = "form-demo"
# title = "Форма"
# version = 1
#
# [[params]]
# name = "query"
# type = "string"
# title = "Запрос"
# description = "что искать"
# required = true
#
# [[params]]
# name = "notes"
# type = "text"
# title = "Заметки"
#
# [[params]]
# name = "pages"
# type = "int"
# title = "Страницы"
# default = 5
# min = 1
# max = 100
#
# [[params]]
# name = "ratio"
# type = "float"
# title = "Доля"
# min = 0
# max = 1
#
# [[params]]
# name = "headless"
# type = "bool"
# title = "Без окна"
# default = true
#
# [[params]]
# name = "sort"
# type = "enum"
# title = "Сортировка"
# choices = ["new", "popular"]
# default = "popular"
#
# [[params]]
# name = "start"
# type = "url"
# title = "Стартовая страница"
#
# [[params]]
# name = "urls"
# type = "url_list"
# title = "Адреса"
# max_items = 3
#
# [[params]]
# name = "tags"
# type = "string_list"
# title = "Метки"
#
# [[params]]
# name = "token"
# type = "secret"
# title = "Токен"
# required = true
# ///
print("hi")
"""


@pytest.fixture
def manifest():
    return parse_manifest(ALL_TYPES)


@pytest.fixture
def registered(app, tmp_path):
    f = tmp_path / "form.py"
    f.write_text(ALL_TYPES, encoding="utf-8")
    app.registry.add(f)
    return "form-demo"


GOOD = {"query": "ноутбук", "token": "tok"}


class MultiDict:
    """Как werkzeug/starlette: getlist."""

    def __init__(self, pairs):
        self.pairs = pairs

    def getlist(self, name):
        return [v for k, v in self.pairs if k == name]


# --- поля ------------------------------------------------------------------------


def test_fields_for_all_types(manifest):
    fields = {f.name: f for f in forms.fields_for(manifest, secret_status={"token": True})}
    assert {n: f.widget for n, f in fields.items()} == {
        "query": "text", "notes": "textarea", "pages": "number", "ratio": "number", "headless": "checkbox",
        "sort": "select", "start": "url", "urls": "textarea", "tags": "textarea", "token": "password",
    }
    assert fields["query"].required and fields["query"].description == "что искать"
    assert (fields["pages"].step, fields["pages"].min, fields["pages"].max, fields["pages"].value) == ("1", 1, 100, "5")
    assert fields["ratio"].step == "any" and fields["ratio"].value == ""
    assert fields["headless"].value is True
    assert fields["sort"].choices == ["new", "popular"] and fields["sort"].value == "popular"
    assert fields["urls"].max_items == 3
    assert fields["token"].secret_set is True and fields["token"].value == "" and fields["token"].default is None
    assert fields["query"].secret_set is None
    json.dumps([f.to_dict() for f in fields.values()], ensure_ascii=False)  # годится для шаблона и JSON


def test_fields_show_values_lists_by_line(manifest):
    fields = {f.name: f for f in forms.fields_for(manifest, values={"urls": ["https://a.ru", "https://b.ru"], "headless": False, "ratio": 0.5})}
    assert fields["urls"].value == "https://a.ru\nhttps://b.ru"
    assert fields["headless"].value is False and fields["ratio"].value == "0.5"


# --- разбор ----------------------------------------------------------------------


def test_parse_all_types(manifest):
    form = {
        "query": "ноутбук",
        "notes": "строка 1\r\nстрока 2",
        "pages": " 7 ",
        "ratio": "0,25",
        "headless": "on",
        "sort": "new",
        "start": "https://ozon.ru",
        "urls": "https://a.ru\r\n\r\n  https://b.ru  \n",
        "tags": "a\n\n b \n",
        "token": "S3CRET-value",
    }
    sub = forms.parse(manifest, form)
    assert sub.ok, sub.errors
    assert sub.values == {
        "query": "ноутбук", "notes": "строка 1\nстрока 2", "pages": 7, "ratio": 0.25, "headless": True,
        "sort": "new", "start": "https://ozon.ru", "urls": ["https://a.ru", "https://b.ru"], "tags": ["a", "b"],
    }
    assert "token" not in sub.values and sub.new_secrets == {"token": "S3CRET-value"}
    assert "S3CRET" not in repr(sub)


def test_bool_absent_is_false(manifest):
    sub = forms.parse(manifest, GOOD)
    assert sub.values["headless"] is False
    assert forms.parse(manifest, MultiDict([("query", "x"), ("token", "t"), ("headless", "on")])).values["headless"] is True


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_means_not_set(manifest, blank):
    form = {**GOOD, "notes": blank, "pages": blank, "ratio": blank, "start": blank, "urls": f"{blank}\n\n", "tags": blank, "sort": blank}
    sub = forms.parse(manifest, form)
    assert sub.ok, sub.errors
    assert sub.values == {"query": "ноутбук", "headless": False}  # остальное не задано, при запуске default


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_required(manifest, blank):
    sub = forms.parse(manifest, {"query": blank, "token": "t"})
    assert sub.errors == {"query": "обязательное поле"}
    assert sub.raw["query"] == blank  # для повторной отрисовки


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("pages", "0", "не меньше 1"),
        ("pages", "101", "не больше 100"),
        ("pages", "2.5", "нужно целое число, введено «2.5»"),
        ("pages", "abc", "нужно целое число, введено «abc»"),
        ("ratio", "1.5", "не больше 1"),
        ("ratio", "-0.1", "не меньше 0"),
        ("ratio", "nan", "нужно обычное число, введено «nan»"),
        ("sort", "random", "выберите значение из списка"),
        ("start", "ozon.ru", "нужен адрес http(s)://..., введено «ozon.ru»"),
        ("urls", "https://a.ru\nftp://b.ru", "элемент 1: нужен адрес http(s)://..., введено «ftp://b.ru»"),
        ("urls", "https://a.ru\nhttps://b.ru\nhttps://c.ru\nhttps://d.ru", "не больше 3 строк, а введено больше"),
        ("query", "две\nстроки", "одна строка, без переводов строки"),
    ],
)
def test_field_errors(manifest, field, value, error):
    sub = forms.parse(manifest, {**GOOD, field: value})
    assert sub.errors == {field: error}
    assert field not in sub.values and sub.raw[field] == value


def test_boundaries_accepted(manifest):
    sub = forms.parse(manifest, {**GOOD, "pages": "1", "ratio": "1", "urls": "https://a.ru\nhttps://b.ru\nhttps://c.ru"})
    assert sub.ok and sub.values["pages"] == 1 and sub.values["ratio"] == 1.0 and len(sub.values["urls"]) == 3


def test_all_errors_at_once(manifest):
    sub = forms.parse(manifest, {"pages": "0", "sort": "x"})
    assert set(sub.errors) == {"pages", "sort", "query", "token"}
    assert sub.errors["token"] == "обязательный секрет не задан"


def test_required_secret_from_keyring(manifest):
    assert forms.parse(manifest, {"query": "x"}, secret_set={"token": True}).ok


def test_multidict_and_lists(manifest):
    md = MultiDict([("query", "старое"), ("query", "новое"), ("token", "t")])
    assert forms.parse(manifest, md).values["query"] == "новое"
    assert forms.parse(manifest, {"query": ["x"], "token": ["t"]}).values["query"] == "x"


def test_fields_keep_input_after_errors(manifest):
    sub = forms.parse(manifest, {**GOOD, "pages": "abc", "headless": "on", "token": "секрет"})
    fields = {f.name: f for f in forms.fields_for(manifest, raw=sub.raw, errors=sub.errors)}
    assert fields["pages"].value == "abc" and fields["pages"].error.startswith("нужно целое")
    assert fields["headless"].value is True
    assert fields["token"].value == ""  # секрет обратно не отдаётся


# --- с приложением: секреты, пресеты, запуск --------------------------------------


def test_submit_saves_secret_and_blank_keeps_it(app, registered, kr):
    sub = forms.submit(app, registered, {"query": "x", "token": "  новый  "})
    assert sub.ok and sub.secrets_saved == ["token"]
    assert kr.store[("dawg-replay", "secret:form-demo:token")] == "новый"
    tok = {f.name: f for f in sub.fields}["token"]
    assert tok.secret_set is True and tok.value == ""
    sub = forms.submit(app, registered, {"query": "x", "token": ""})
    assert sub.ok and sub.secrets_saved == []
    assert kr.store[("dawg-replay", "secret:form-demo:token")] == "новый"  # пустое поле не стирает


def test_submit_saves_secret_even_with_other_errors(app, registered, kr):
    sub = forms.submit(app, registered, {"query": "", "token": "t1"})
    assert sub.errors == {"query": "обязательное поле"}
    assert sub.secrets_saved == ["token"] and kr.store[("dawg-replay", "secret:form-demo:token")] == "t1"
    assert "token" not in sub.errors  # повторно вводить секрет не нужно


def test_submit_missing_required_secret(app, registered):
    sub = forms.submit(app, registered, {"query": "x"})
    assert sub.errors == {"token": "обязательный секрет не задан"}


def test_preset_fill_and_save(app, registered, kr):
    sub = forms.save_preset(app, registered, "base", {"query": "из формы", "pages": "3", "headless": "on", "token": "секрет"})
    assert sub.ok
    preset = app.get_preset(registered, "base")
    assert preset.params == {"query": "из формы", "pages": 3, "headless": True}  # без секрета
    assert ("dawg-replay", "secret:form-demo:token") not in kr.store
    # Пресет без обязательных полей сохраняется, невалидный — нет.
    assert forms.save_preset(app, registered, "partial", {"pages": "4"}).ok
    bad = forms.save_preset(app, registered, "bad", {"pages": "0"})
    assert bad.errors == {"pages": "не меньше 1"}
    with pytest.raises(Exception):
        app.get_preset(registered, "bad")
    built = forms.build_fields(app, registered, preset="base")
    fields = {f.name: f for f in built["fields"]}
    assert fields["query"].value == "из формы" and fields["pages"].value == "3" and fields["headless"].value is True
    assert built["dropped"] == []


def test_preset_dropped_shown(app, registered, tmp_path):
    app.db.execute(
        "INSERT INTO presets (script_name, name, params_json, created_at, updated_at) VALUES (?, ?, ?, 'x', 'x')",
        (registered, "old", json.dumps({"query": "q", "removed": 1, "pages": 0})),
    )
    built = forms.build_fields(app, registered, preset="old")
    assert sorted(built["dropped"]) == ["pages", "removed"]
    assert {f.name: f.value for f in built["fields"]}["query"] == "q"


def test_bad_preset_name(app, registered):
    sub = forms.save_preset(app, registered, "плохое имя!", {"query": "x"})
    assert forms.FORM_ERROR in sub.errors


def test_valid_form_goes_to_prepare(app, registered, kr, monkeypatch):
    monkeypatch.setenv(launch.ENV_SCRIPT_COMMAND, json.dumps([sys.executable]))
    sub = forms.submit(app, registered, {"query": "ноутбук", "pages": "9", "token": "tok", "urls": "https://a.ru"})
    assert sub.ok
    prepared = launch.prepare(app, registered, values=sub.values)
    assert prepared.values["query"] == "ноутбук" and prepared.values["pages"] == 9
    assert prepared.values["headless"] is False and prepared.values["urls"] == ["https://a.ru"]
    assert prepared.values["token"] == "tok"  # секрет prepare взял из keyring
    assert "token" not in prepared.run.params  # и в БД его нет


def test_invalid_form_does_not_reach_prepare(app, registered):
    sub = forms.submit(app, registered, {"query": "x", "token": "t", "pages": "0"})
    assert not sub.ok and sub.values.get("pages") is None
    assert app.runs.list() == []
