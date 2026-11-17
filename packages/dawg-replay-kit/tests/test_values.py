"""Выходные значения (контракт 1.5): `[[outputs]]` с kind = "value" и kit.output."""

import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, time

import pytest
from dawg_replay_kit import __version__
from dawg_replay_kit.manifest import ManifestError, contract_versions, parse_manifest
from dawg_replay_kit.runtime import Kit

HEAD = '# /// script\n# requires-python = ">=3.10"\n# dependencies = ["dawg-replay-kit>=1.5,<2"]\n# ///\n'

OUTPUTS = '''\
#
# [[outputs]]
# name = "orders.csv"
# columns = ["id"]
#
# [[outputs]]
# kind = "value"
# name = "token"
# title = "Токен"
# type = "string"
# secret = true
#
# [[outputs]]
# kind = "value"
# name = "count"
# type = "int"
#
# [[outputs]]
# kind = "value"
# name = "day"
# type = "date"
# optional = true
#
# [[outputs]]
# kind = "value"
# name = "mode"
# type = "enum"
# choices = ["fast", "slow"]
# optional = true
#
# [[outputs]]
# kind = "value"
# name = "links"
# type = "url_list"
# optional = true
'''


def manifest_text(contract="1.5", outputs=OUTPUTS, body=""):
    return (HEAD + f'\n# /// dawg-script\n# contract = "{contract}"\n# name = "vals"\n# title = "Значения"\n'
            f'# version = 1\n{outputs}# ///\n{body}')


def issues(text):
    with pytest.raises(ManifestError) as e:
        parse_manifest(text)
    return {(i.code, i.path) for i in e.value.issues}


# --- манифест ---


def test_versions():
    assert __version__ == "1.5.0"
    assert contract_versions() == ["1.5"]


def test_value_outputs_parsed():
    m = parse_manifest(manifest_text())
    assert [o.name for o in m.files_out()] == ["orders.csv"]
    assert [(o.name, o.type, o.secret, o.optional) for o in m.values_out()] == [
        ("token", "string", True, False), ("count", "int", False, False), ("day", "date", False, True),
        ("mode", "enum", False, True), ("links", "url_list", False, True)]
    assert m.value_out("mode").choices == ("fast", "slow")
    assert m.value_out("token").is_value and not m.files_out()[0].is_value
    with pytest.raises(KeyError):
        m.value_out("orders.csv")
    assert m.value_out("token").to_dict() == {"name": "token", "kind": "value", "type": "string", "title": "Токен",
                                             "secret": True}
    assert m.files_out()[0].to_dict() == {"name": "orders.csv", "columns": ["id"]}  # kind у файла не пишется
    assert m.to_dict()["outputs"][2] == {"name": "count", "kind": "value", "type": "int"}


def test_value_output_check():
    m = parse_manifest(manifest_text())
    assert m.value_out("count").check(5) is None
    assert "целое" in m.value_out("count").check("5")
    assert m.value_out("day").check("2026-11-17") is None
    assert "выражение" in m.value_out("day").check("today")
    assert "нет в choices" in m.value_out("mode").check("medium")
    assert m.value_out("links").check(["https://a.ru"]) is None


def test_file_output_with_kind_file():
    text = manifest_text(outputs='#\n# [[outputs]]\n# kind = "file"\n# name = "a.csv"\n')
    assert parse_manifest(text).outputs[0].kind == "file"


def test_value_needs_contract_15():
    got = issues(manifest_text(contract="1.4"))
    assert ("field_needs_minor", "outputs[1].kind") in got
    # файлы 1.4 без kind по-прежнему работают
    assert parse_manifest(manifest_text(contract="1.4", outputs='#\n# [[outputs]]\n# name = "a.csv"\n'))


def test_value_output_errors():
    bad = '''\
#
# [[outputs]]
# kind = "value"
# name = "Bad-Name"
# type = "table"
# columns = ["x"]
#
# [[outputs]]
# kind = "value"
# name = "x"
#
# [[outputs]]
# kind = "value"
# name = "x"
# type = "string"
# secret = "yes"
# choices = ["a"]
#
# [[outputs]]
# kind = "value"
# name = "e"
# type = "enum"
#
# [[outputs]]
# kind = "thing"
# name = "y.csv"
'''
    got = issues(manifest_text(outputs=bad))
    for want in [("bad_value", "outputs[0].name"), ("bad_type", "outputs[0].type"), ("unknown_field", "outputs[0].columns"),
                 ("missing_field", "outputs[1].type"), ("duplicate_output", "outputs[2].name"),
                 ("bad_value", "outputs[2].secret"), ("bad_value", "outputs[2].choices"),
                 ("bad_value", "outputs[3].choices"), ("bad_value", "outputs[4].kind")]:
        assert want in got, want


def test_value_and_file_names_separate():
    text = manifest_text(outputs='#\n# [[outputs]]\n# name = "token"\n#\n# [[outputs]]\n# kind = "value"\n'
                                 '# name = "token"\n# type = "string"\n')
    m = parse_manifest(text)
    assert [o.kind for o in m.outputs] == ["file", "value"]


# --- kit.output ---


@pytest.fixture
def script(tmp_path):
    def write(body=""):
        f = tmp_path / "vals.py"
        f.write_text(manifest_text(body=body), encoding="utf-8")
        return f

    return write


def app_kit(script_path, tmp_path, extra=None):
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    env = {"DAWG_SCRIPT_PARAMS": str(params), "DAWG_SCRIPT_EVENTS": str(tmp_path / "run" / "events.jsonl"),
           "DAWG_SCRIPT_OUTPUT_DIR": str(tmp_path / "run" / "output"), **(extra or {})}
    (tmp_path / "run").mkdir(exist_ok=True)
    return Kit.from_environment(script_path=script_path, argv=[], environ=env)


def events(tmp_path):
    return [json.loads(x) for x in (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_output_writes_file_and_events(script, tmp_path):
    k = app_kit(script(), tmp_path)
    k.values.output("count", 3)
    k.values.output("count", 4)  # повтор заменяет
    k.values.output("token", "tok-SECRET-1")
    k.values.output("day", date(2026, 11, 17))
    k.values.output("links", ("https://a.ru",))
    k.events.log("info", "получен tok-SECRET-1")
    k.close()
    path = tmp_path / "run" / "values.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "count": 4, "token": "tok-SECRET-1", "day": "2026-11-17", "links": ["https://a.ru"]}
    if sys.platform != "win32":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    evs = events(tmp_path)
    outs = [e for e in evs if e["t"] == "output"]
    assert [(e["name"], e.get("value"), e.get("secret")) for e in outs] == [
        ("count", 3, None), ("count", 4, None), ("token", None, True), ("day", "2026-11-17", None),
        ("links", ["https://a.ru"], None)]
    raw = (tmp_path / "run" / "events.jsonl").read_text(encoding="utf-8")
    assert "tok-SECRET-1" not in raw  # в журнале нет и дальше маскируется


def test_output_values_path_from_env(script, tmp_path):
    k = app_kit(script(), tmp_path, {"DAWG_SCRIPT_VALUES": str(tmp_path / "custom" / "v.json")})
    k.values.output("count", 1)
    k.close()
    assert json.loads((tmp_path / "custom" / "v.json").read_text(encoding="utf-8")) == {"count": 1}


def test_output_errors(script, tmp_path):
    k = app_kit(script(), tmp_path)
    with pytest.raises(ValueError, match="не объявлено"):
        k.values.output("orders.csv", 1)
    with pytest.raises(ValueError, match="целое"):
        k.values.output("count", "много")
    with pytest.raises(ValueError, match="нет в choices"):
        k.values.output("mode", "medium")
    with pytest.raises(ValueError, match="выражение"):
        k.values.output("day", "today")
    k.close()
    assert not (tmp_path / "run" / "values.json").exists()


def test_output_moments():
    from dawg_replay_kit.values import _plain

    assert _plain(datetime(2026, 11, 17, 9, 30), "datetime") == "2026-11-17T09:30"
    assert _plain(time(9, 5), "time") == "09:05"
    assert _plain(date(2026, 1, 2), "string") == date(2026, 1, 2)  # не тот тип: проверка скажет


def test_autonomous_prints_values(script, tmp_path):
    stream = io.StringIO()
    k = Kit.from_environment(script_path=script(), argv=[], environ={}, stream=stream, cwd=tmp_path)
    k.values.output("count", 2)
    k.values.output("token", "zzz-secret")
    k.close()
    text = stream.getvalue()
    assert "значение «count»: 2" in text and "значение «Токен»: скрыто" in text and "zzz-secret" not in text
    assert not list(tmp_path.rglob("values.json"))


def test_kit_output_in_script(script, tmp_path):
    body = 'import dawg_replay_kit as kit\nkit.output("count", 7)\nkit.result("ok", "готово")\n'
    path = script(body)
    params = tmp_path / "p.json"
    params.write_text("{}", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DAWG_SCRIPT_")}
    env.update({"DAWG_SCRIPT_PARAMS": str(params), "DAWG_SCRIPT_EVENTS": str(tmp_path / "ev.jsonl"),
                "DAWG_SCRIPT_OUTPUT_DIR": str(tmp_path / "out")})
    r = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, encoding="utf-8", env=env, check=False)
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "values.json").read_text(encoding="utf-8")) == {"count": 7}
