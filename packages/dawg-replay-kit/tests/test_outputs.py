"""Контракт 1.4: блок `[[outputs]]`, объявленные файлы результата."""

import textwrap

import pytest

from dawg_replay_kit.manifest import ManifestError, Output, contract_versions, parse_manifest

SCRIPT = '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["dawg-replay-kit>=1.4,<2"]\n# ///\n'


def manifest(tail: str, contract: str = '"1.4"') -> str:
    body = textwrap.dedent(f"""\
        contract = {contract}
        name = "prices"
        title = "Цены"
        version = 1
        """) + textwrap.dedent(tail)
    lines = "\n".join(f"# {x}" if x else "#" for x in body.strip("\n").splitlines())
    return f"{SCRIPT}\n# /// dawg-script\n{lines}\n# ///\n"


def codes(text):
    with pytest.raises(ManifestError) as e:
        parse_manifest(text)
    return [(i.code, i.path) for i in e.value.issues]


def test_outputs_parsed():
    m = parse_manifest(manifest("""
        [[outputs]]
        name = "prices.csv"
        title = "Цены"
        columns = ["sku", "price"]
        main = true

        [[outputs]]
        name = "shots/page-*.png"
        """))
    assert m.outputs == (
        Output("prices.csv", "Цены", ("sku", "price"), True),
        Output("shots/page-*.png"),
    )
    assert m.main_output.name == "prices.csv"
    assert m.outputs[1].label == "shots/page-*.png" and m.outputs[1].is_pattern
    assert m.to_dict()["outputs"] == [
        {"name": "prices.csv", "title": "Цены", "columns": ["sku", "price"], "main": True},
        {"name": "shots/page-*.png"},
    ]
    assert contract_versions() == ["1.4"]


def test_optional_output():
    m = parse_manifest(manifest("""
        [[outputs]]
        name = "yes.csv"
        optional = true

        [[outputs]]
        name = "no.csv"
        optional = false
        """))
    assert [o.optional for o in m.outputs] == [True, False]
    assert m.to_dict()["outputs"] == [{"name": "yes.csv", "optional": True}, {"name": "no.csv"}]


def test_no_outputs_as_before():
    m = parse_manifest(manifest("", contract='"1.3"'))
    assert m.outputs == () and m.main_output is None and "outputs" not in m.to_dict()
    assert parse_manifest(manifest("", contract="1")).outputs == ()


def test_outputs_need_contract_14():
    text = manifest('[[outputs]]\nname = "a.csv"\n', contract='"1.3"')
    assert ("field_needs_minor", "outputs") in codes(text)


def test_matches():
    exact, pat, sub = Output("prices.csv"), Output("prices-*.csv"), Output("out/*.json")
    assert exact.matches("prices.csv") and not exact.matches("x/prices.csv")
    assert pat.matches("prices-2026.csv") and not pat.matches("prices.csv") and not pat.matches("a/prices-1.csv")
    assert sub.matches("out/a.json") and not sub.matches("out/deep/a.json") and not sub.matches("a.json")


@pytest.mark.parametrize("tail, expected", [
    ('outputs = "a.csv"\n', ("bad_value", "outputs")),
    ('outputs = ["a.csv"]\n', ("output_not_table", "outputs[0]")),
    ('[[outputs]]\ntitle = "x"\n', ("missing_field", "outputs[0].name")),
    ('[[outputs]]\nname = "/etc/passwd"\n', ("bad_value", "outputs[0].name")),
    ('[[outputs]]\nname = "../a.csv"\n', ("bad_value", "outputs[0].name")),
    ('[[outputs]]\nname = "a//b.csv"\n', ("bad_value", "outputs[0].name")),
    ('[[outputs]]\nname = "a\\\\b.csv"\n', ("bad_value", "outputs[0].name")),
    ('[[outputs]]\nname = ""\n', ("bad_value", "outputs[0].name")),
    ('[[outputs]]\nname = "a.csv"\nformat = "csv"\n', ("unknown_field", "outputs[0](a.csv).format")),
    ('[[outputs]]\nname = "a.csv"\ntitle = ""\n', ("bad_value", "outputs[0](a.csv).title")),
    ('[[outputs]]\nname = "a.csv"\ncolumns = []\n', ("bad_value", "outputs[0](a.csv).columns")),
    ('[[outputs]]\nname = "a.csv"\ncolumns = ["a", "a"]\n', ("bad_value", "outputs[0](a.csv).columns")),
    ('[[outputs]]\nname = "a.csv"\nmain = "yes"\n', ("bad_value", "outputs[0](a.csv).main")),
    ('[[outputs]]\nname = "a.csv"\noptional = 1\n', ("bad_value", "outputs[0](a.csv).optional")),
    ('[[outputs]]\nname = "a.csv"\n\n[[outputs]]\nname = "a.csv"\n', ("duplicate_output", "outputs[1].name")),
    ('[[outputs]]\nname = "a.csv"\nmain = true\n\n[[outputs]]\nname = "b.csv"\nmain = true\n',
     ("duplicate_main", "outputs[1](b.csv).main")),
])
def test_bad_outputs(tail, expected):
    assert expected in codes(manifest(tail))


def test_too_many_outputs():
    tail = "".join(f'[[outputs]]\nname = "f{i}.csv"\n\n' for i in range(51))
    assert ("bad_value", "outputs") in codes(manifest(tail))
