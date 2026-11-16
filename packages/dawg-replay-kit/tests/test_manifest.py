import textwrap

import pytest

from dawg_replay_kit.manifest import (
    PARAM_TYPES,
    ManifestError,
    check_value,
    load_manifest,
    parse_manifest,
)

SCRIPT_BLOCK = """\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2", "dawg-sdk-python>=0.4,<0.5", "playwright>=1.58,<2"]
# ///
"""

SPEC_EXAMPLE = SCRIPT_BLOCK + """
# /// dawg-script
# contract = 1
# name = "ozon-search-cards"          # slug, уникальный в приложении
# title = "Ozon: карточки по поисковому запросу"
# description = "Обходит выдачу Ozon по запросу и собирает название, цену, рейтинг и продавца."
# version = 3                         # растёт при каждой правке
# timeout_minutes = 60                # предел длительности одного запуска
#
# [[params]]
# name = "query"
# type = "string"
# title = "Поисковый запрос"
# required = true
#
# [[params]]
# name = "max_pages"
# type = "int"
# title = "Сколько страниц выдачи обойти"
# default = 5
# min = 1
# max = 100
#
# [[params]]
# name = "sheet_token"
# type = "secret"
# title = "Токен Google Sheets"
# ///

import dawg_replay_kit as kit
"""


def make(manifest_body: str, script: str = SCRIPT_BLOCK) -> str:
    body = textwrap.dedent(manifest_body).strip("\n")
    lines = "\n".join(f"# {line}" if line else "#" for line in body.splitlines())
    return f"{script}\n# /// dawg-script\n{lines}\n# ///\n\nprint('hi')\n"


HEAD = """\
contract = 1
name = "demo"
title = "Демо"
version = 1
"""


def with_param(param_toml: str) -> str:
    return make(HEAD + "\n[[params]]\n" + textwrap.dedent(param_toml).strip("\n"))


def errors(text: str) -> list:
    with pytest.raises(ManifestError) as e:
        parse_manifest(text)
    return e.value.issues


def codes(text: str) -> list[str]:
    return [i.code for i in errors(text)]


# --- корректные манифесты ---------------------------------------------------


def test_spec_example():
    m = parse_manifest(SPEC_EXAMPLE)
    assert m.contract == 1 and m.contract_minor == 0
    assert m.name == "ozon-search-cards"
    assert m.version == 3
    assert m.timeout_minutes == 60
    assert m.description.startswith("Обходит")
    assert [p.name for p in m.params] == ["query", "max_pages", "sheet_token"]
    assert m.param("query").required is True
    mp = m.param("max_pages")
    assert (mp.type, mp.default, mp.min, mp.max) == ("int", 5, 1, 100)
    assert m.param("sheet_token").type == "secret"
    assert m.requires_python == ">=3.10"
    assert "playwright>=1.58,<2" in m.dependencies


def test_minimal_defaults():
    m = parse_manifest(make(HEAD))
    assert m.params == ()
    assert m.timeout_minutes == 60
    assert m.description is None


def test_contract_minor_string():
    m = parse_manifest(make(HEAD.replace("contract = 1", 'contract = "1.2"')))
    assert (m.contract, m.contract_minor) == (1, 2)


@pytest.mark.parametrize(
    "param, default",
    [
        ('type = "string"\ndefault = "ноутбук"', "ноутбук"),
        ('type = "text"\ndefault = "строка 1\\nстрока 2"', "строка 1\nстрока 2"),
        ('type = "int"\ndefault = 3\nmin = 1\nmax = 5', 3),
        ('type = "float"\ndefault = 2\nmin = 0.5\nmax = 2.5', 2),
        ('type = "float"\ndefault = 1.5', 1.5),
        ('type = "bool"\ndefault = false', False),
        ('type = "enum"\nchoices = ["new", "popular"]\ndefault = "popular"', "popular"),
        ('type = "url"\ndefault = "https://www.ozon.ru/category/x"', "https://www.ozon.ru/category/x"),
        ('type = "url_list"\ndefault = ["https://a.ru", "http://b.ru/x"]\nmax_items = 5', ["https://a.ru", "http://b.ru/x"]),
        ('type = "string_list"\ndefault = []\nmax_items = 2', []),
        ('type = "secret"', None),
    ],
)
def test_every_param_type(param, default):
    m = parse_manifest(with_param(f'name = "p"\ntitle = "П"\n{param}'))
    (p,) = m.params
    assert p.default == default
    assert p.required is False


def test_all_types_covered():
    covered = {"string", "text", "int", "float", "bool", "enum", "url", "url_list", "string_list", "secret", "date", "table", "time", "datetime", "multi_enum", "file"}
    assert covered == set(PARAM_TYPES)


def test_to_dict_is_json_ready():
    import json

    d = parse_manifest(SPEC_EXAMPLE).to_dict()
    json.dumps(d)
    assert d["params"][1] == {
        "name": "max_pages",
        "type": "int",
        "title": "Сколько страниц выдачи обойти",
        "required": False,
        "default": 5,
        "min": 1,
        "max": 100,
    }


# --- блоки ------------------------------------------------------------------


def test_no_manifest_block():
    assert codes(SCRIPT_BLOCK + "\nprint(1)\n") == ["missing_block"]


def test_no_script_block():
    assert codes(make(HEAD, script="")) == ["missing_block"]


def test_unclosed_manifest_block():
    text = SCRIPT_BLOCK + "\n# /// dawg-script\n# contract = 1\n\nprint(1)\n"
    issues = errors(text)
    assert [i.code for i in issues] == ["unclosed_block"]
    assert issues[0].line == 6


def test_duplicate_block():
    text = make(HEAD) + "\n# /// dawg-script\n# contract = 1\n# ///\n"
    assert "duplicate_block" in codes(text)


def test_toml_syntax_error_points_to_file_line():
    text = make(HEAD + 'title = "дубль"\n')
    issues = errors(text)
    assert issues[0].code == "toml_syntax"
    # Блок script занимает строки 1-4, пустая 5, заголовок 6, `title = "дубль"` на 11.
    assert issues[0].line == 11


# --- ошибки верхнего уровня --------------------------------------------------


def test_missing_required_fields_reported_together():
    issues = errors(make("description = 'x'"))
    assert sorted(i.path for i in issues) == ["contract", "name", "title", "version"]
    assert {i.code for i in issues} == {"missing_field"}


@pytest.mark.parametrize("contract", ["2", '"2.0"'])
def test_unsupported_contract(contract):
    issues = errors(make(HEAD.replace("contract = 1", f"contract = {contract}")))
    assert [i.code for i in issues] == ["unsupported_contract"]
    assert "контракт 2" in issues[0].message


@pytest.mark.parametrize("contract", ['"1"', "1.0", "true", '"v1"', '"1.1\\n"'])
def test_bad_contract_value(contract):
    assert codes(make(HEAD.replace("contract = 1", f"contract = {contract}"))) == ["bad_value"]


@pytest.mark.parametrize(
    "name", ['"Ozon"', '"ozon_cards"', '"-ozon"', '"ozon--x"', '""', "5", f'"{"a" * 65}"', '"ozon\\n"', '"ozon\\n/../x"']
)
def test_bad_slug(name):
    assert codes(make(HEAD.replace('name = "demo"', f"name = {name}"))) == ["bad_slug"]


@pytest.mark.parametrize("version", ["0", "-1", '"3"', "1.5", "true"])
def test_bad_version(version):
    assert codes(make(HEAD.replace("version = 1", f"version = {version}"))) == ["bad_value"]


@pytest.mark.parametrize("timeout", ["0", '"60"', "1.5"])
def test_bad_timeout(timeout):
    assert codes(make(HEAD + f"timeout_minutes = {timeout}")) == ["bad_value"]


def test_unknown_top_field():
    issues = errors(make(HEAD + "timout_minutes = 5"))
    assert [(i.code, i.path) for i in issues] == [("unknown_field", "timout_minutes")]


def test_empty_title():
    assert codes(make(HEAD.replace('title = "Демо"', 'title = "  "'))) == ["bad_value"]


def test_params_not_array_of_tables():
    assert codes(make(HEAD + 'params = "x"')) == ["bad_value"]


def test_bad_script_block_dependencies():
    script = '# /// script\n# dependencies = "playwright"\n# ///\n'
    assert codes(make(HEAD, script=script)) == ["bad_value"]


# --- ошибки параметров -------------------------------------------------------


@pytest.mark.parametrize(
    "param, code, path",
    [
        ('type = "string"\ntitle = "x"', "missing_field", "params[0].name"),
        ('name = "q"\ntitle = "x"', "missing_field", "params[0](q).type"),
        ('name = "q"\ntype = "string"', "missing_field", "params[0](q).title"),
        ('name = "Query"\ntype = "string"\ntitle = "x"', "bad_param_name", "params[0].name"),
        ('name = "max-pages"\ntype = "string"\ntitle = "x"', "bad_param_name", "params[0].name"),
        ('name = "query\\n"\ntype = "string"\ntitle = "x"', "bad_param_name", "params[0].name"),
        ('name = "params"\ntype = "string"\ntitle = "x"', "reserved_param_name", "params[0].name"),
        ('name = "q"\ntype = "timestamp"\ntitle = "x"', "bad_type", "params[0](q).type"),
        ('name = "q"\ntype = "string"\ntitle = "x"\nrequired = "yes"', "bad_value", "params[0](q).required"),
        ('name = "q"\ntype = "string"\ntitle = "x"\nlabel = "y"', "unknown_field", "params[0](q).label"),
        ('name = "q"\ntype = "string"\ntitle = "x"\nmin = 1', "field_not_allowed", "params[0](q).min"),
        ('name = "q"\ntype = "string"\ntitle = "x"\nmax_items = 3', "field_not_allowed", "params[0](q).max_items"),
        ('name = "q"\ntype = "string"\ntitle = "x"\nchoices = ["a"]', "field_not_allowed", "params[0](q).choices"),
        ('name = "q"\ntype = "int"\ntitle = "x"\nmin = 1.5', "bad_value", "params[0](q).min"),
        ('name = "q"\ntype = "int"\ntitle = "x"\nmin = 10\nmax = 1', "bad_range", "params[0](q).min"),
        ('name = "q"\ntype = "url_list"\ntitle = "x"\nmax_items = 0', "bad_value", "params[0](q).max_items"),
        ('name = "q"\ntype = "enum"\ntitle = "x"', "missing_field", "params[0](q).choices"),
        ('name = "q"\ntype = "enum"\ntitle = "x"\nchoices = []', "bad_value", "params[0](q).choices"),
        ('name = "q"\ntype = "enum"\ntitle = "x"\nchoices = ["a", "a"]', "bad_value", "params[0](q).choices"),
        ('name = "q"\ntype = "enum"\ntitle = "x"\nchoices = [1, 2]', "bad_value", "params[0](q).choices"),
        ('name = "q"\ntype = "secret"\ntitle = "x"\ndefault = "tok"', "field_not_allowed", "params[0](q).default"),
    ],
)
def test_param_errors(param, code, path):
    issues = errors(with_param(param))
    assert [(i.code, i.path) for i in issues] == [(code, path)]


@pytest.mark.parametrize(
    "param",
    [
        'type = "string"\ndefault = 5',
        'type = "string"\ndefault = "a\\nb"',
        'type = "text"\ndefault = ["a"]',
        'type = "int"\ndefault = 1.5',
        'type = "int"\ndefault = true',
        'type = "int"\ndefault = 0\nmin = 1',
        'type = "int"\ndefault = 101\nmax = 100',
        'type = "float"\ndefault = "1.5"',
        'type = "bool"\ndefault = "true"',
        'type = "enum"\nchoices = ["a", "b"]\ndefault = "c"',
        'type = "url"\ndefault = "ozon.ru"',
        'type = "url"\ndefault = "ftp://ozon.ru"',
        'type = "url"\ndefault = "https://"',
        'type = "url_list"\ndefault = "https://a.ru"',
        'type = "url_list"\ndefault = ["https://a.ru", "a.ru"]',
        'type = "url_list"\ndefault = ["https://a.ru", "https://b.ru"]\nmax_items = 1',
        'type = "string_list"\ndefault = ["a", 1]',
    ],
)
def test_bad_default(param):
    issues = errors(with_param(f'name = "p"\ntitle = "П"\n{param}'))
    assert [(i.code, i.path) for i in issues] == [("bad_default", "params[0](p).default")]


def test_duplicate_param_name():
    text = make(HEAD + '\n[[params]]\nname = "q"\ntype = "string"\ntitle = "a"\n\n[[params]]\nname = "q"\ntype = "int"\ntitle = "b"')
    issues = errors(text)
    assert [(i.code, i.path) for i in issues] == [("duplicate_param", "params[1].name")]


def test_errors_from_several_params_collected():
    text = make(
        HEAD.replace("version = 1", "version = 0")
        + '\n[[params]]\nname = "a"\ntype = "nope"\ntitle = "a"\n\n[[params]]\nname = "b"\ntype = "int"\ntitle = "b"\ndefault = "x"'
    )
    assert codes(text) == ["bad_value", "bad_type", "bad_default"]


# --- check_value и файлы ------------------------------------------------------


def test_check_value_for_runtime_values():
    m = parse_manifest(SPEC_EXAMPLE)
    assert check_value(m.param("max_pages"), 7) is None
    assert "больше max" in check_value(m.param("max_pages"), 500)
    assert check_value(m.param("query"), "ноутбук") is None


def test_load_manifest_from_file(tmp_path):
    f = tmp_path / "script.py"
    f.write_text(SPEC_EXAMPLE, encoding="utf-8")
    assert load_manifest(f).name == "ozon-search-cards"


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(ManifestError) as e:
        load_manifest(tmp_path / "nope.py")
    assert e.value.issues[0].code == "file_not_found"


def test_load_manifest_not_utf8(tmp_path):
    f = tmp_path / "script.py"
    f.write_bytes("# привет".encode("cp1251"))
    with pytest.raises(ManifestError) as e:
        load_manifest(f)
    assert e.value.issues[0].code == "bad_encoding"


def test_crlf_line_endings():
    m = parse_manifest(SPEC_EXAMPLE.replace("\n", "\r\n"))
    assert m.name == "ozon-search-cards"


def test_bom_in_text():
    assert parse_manifest("\ufeff" + SPEC_EXAMPLE).name == "ozon-search-cards"


def test_bom_in_file(tmp_path):
    f = tmp_path / "script.py"
    f.write_bytes(SPEC_EXAMPLE.encode("utf-8-sig"))
    assert load_manifest(f).name == "ozon-search-cards"


# --- контракт 1.1: site и tags ---------------------------------------------

HEAD_11 = HEAD.replace("contract = 1", 'contract = "1.1"')


def test_site_and_tags():
    m = parse_manifest(make(HEAD_11 + 'site = "Яндекс Маркет"\ntags = ["Цены", "Отзывы"]'))
    assert (m.contract, m.contract_minor) == (1, 1)
    assert m.site == "Яндекс Маркет" and m.tags == ("Цены", "Отзывы")
    d = m.to_dict()
    assert d["site"] == "Яндекс Маркет" and d["tags"] == ["Цены", "Отзывы"]


def test_site_and_tags_optional():
    for head in (HEAD, HEAD_11):
        m = parse_manifest(make(head))
        assert m.site is None and m.tags == ()
        assert "site" not in m.to_dict() and "tags" not in m.to_dict()


@pytest.mark.parametrize("field", ['site = "Ozon"', 'tags = ["Цены"]'])
def test_new_fields_need_contract_11(field):
    issues = errors(make(HEAD + field))
    assert [i.code for i in issues] == ["field_needs_minor"]
    assert 'contract = "1.1"' in issues[0].message


def test_new_fields_with_newer_minor():
    m = parse_manifest(make(HEAD.replace("contract = 1", 'contract = "1.3"') + 'site = "Ozon"'))
    assert m.site == "Ozon"


def test_unknown_field_from_newer_minor_hints_upgrade():
    issues = errors(make(HEAD.replace("contract = 1", 'contract = "1.7"') + 'region = "x"'))
    assert [i.code for i in issues] == ["unknown_field"]
    assert "1.7" in issues[0].message and "обновите dawg-replay" in issues[0].message
    issues = errors(make(HEAD_11 + 'region = "x"'))
    assert "обновите" not in issues[0].message


@pytest.mark.parametrize(
    "value",
    ['""', '"  "', '"a\\nb"', '"' + "x" * 41 + '"', "1", '["Ozon"]'],
)
def test_bad_site(value):
    issues = errors(make(HEAD_11 + f"site = {value}"))
    assert [(i.code, i.path) for i in issues] == [("bad_value", "site")]


def test_site_at_limit():
    assert parse_manifest(make(HEAD_11 + 'site = "' + "я" * 40 + '"')).site == "я" * 40


@pytest.mark.parametrize(
    "value, fragment",
    [
        ('"Цены"', "списком строк"),
        ('["a", "b", "c", "d", "e", "f"]', "не больше 5"),
        ('[""]', "tags[0]"),
        ('["ok", " "]', "tags[1]"),
        ('["' + "x" * 25 + '"]', "до 24 символов"),
        ('["a\\nb"]', "в одну строку"),
        ("[1]", "tags[0]"),
        ('["Цены", "Цены"]', "повторяется"),
    ],
)
def test_bad_tags(value, fragment):
    issues = errors(make(HEAD_11 + f"tags = {value}"))
    assert [(i.code, i.path) for i in issues] == [("bad_value", "tags")]
    assert fragment in issues[0].message


def test_tags_at_limits():
    tags = ", ".join(f'"{c * 24}"' for c in "abcde")
    assert len(parse_manifest(make(HEAD_11 + f"tags = [{tags}]")).tags) == 5
    assert parse_manifest(make(HEAD_11 + "tags = []")).tags == ()


def test_contract_versions():
    from dawg_replay_kit.manifest import contract_versions

    assert contract_versions() == ["1.4"]
