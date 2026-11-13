import json

import pytest

from dawg_replay_kit.parameters import ParamsError, build_arg_parser, from_cli, from_file


def cli(manifest, argv, env=None):
    return from_cli(manifest, argv, env or {})


def test_defaults_and_missing_optional(manifest):
    p = cli(manifest, ["--query", "ноутбук"])
    assert p.query == "ноутбук"
    assert p.max_pages == 5
    assert p.headless is True
    assert p.ratio is None and p["sort"] is None and p.urls is None
    assert p.get("ratio", 0.5) == 0.5
    assert p.to_dict(mask_secrets=False) == {"query": "ноутбук", "max_pages": 5, "headless": True}


def test_every_type_from_cli(manifest):
    p = cli(
        manifest,
        [
            "--query", "q",
            "--max-pages", "3",
            "--ratio", "0.5",
            "--headless", "false",
            "--sort", "new",
            "--start-url", "https://ozon.ru",
            "--urls", "https://a.ru", "https://b.ru",
            "--urls", "https://c.ru",
            "--tags", "x",
            "--notes", "две\nстроки",
        ],
        {"DAWG_SECRET_SHEET_TOKEN": "tok-123"},
    )
    assert p.max_pages == 3
    assert p.ratio == 0.5
    assert p.headless is False
    assert p.sort == "new"
    assert p.start_url == "https://ozon.ru"
    assert p.urls == ["https://a.ru", "https://b.ru", "https://c.ru"]
    assert p.tags == ["x"]
    assert p.notes == "две\nстроки"
    assert p.sheet_token == "tok-123"
    assert p.secret_values() == ["tok-123"]
    assert p.to_dict()["sheet_token"] == "***"
    assert "tok-123" not in repr(p)


def test_missing_required_names_flag(manifest):
    with pytest.raises(ParamsError) as e:
        cli(manifest, [])
    assert "--query" in str(e.value)
    assert "«Запрос»" in str(e.value)


def test_missing_required_secret_names_env(demo_text):
    from dawg_replay_kit.manifest import parse_manifest

    m = parse_manifest(demo_text.replace('# title = "Токен"', '# title = "Токен"\n# required = true'))
    with pytest.raises(ParamsError) as e:
        cli(m, ["--query", "q"])
    assert "DAWG_SECRET_SHEET_TOKEN" in str(e.value)


def test_no_cli_flag_for_secret(manifest):
    with pytest.raises(ParamsError) as e:
        cli(manifest, ["--query", "q", "--sheet-token", "x"])
    assert "unrecognized arguments" in str(e.value)


@pytest.mark.parametrize(
    "argv, fragment",
    [
        (["--max-pages", "abc"], "ожидается целое"),
        (["--max-pages", "500"], "больше max"),
        (["--ratio", "x"], "ожидается число"),
        (["--headless", "maybe"], "ожидается true или false"),
        (["--sort", "cheap"], "нет в choices"),
        (["--start-url", "ozon.ru"], "http(s)"),
        (["--urls", "https://a.ru", "https://b.ru", "https://c.ru", "https://d.ru"], "max_items"),
        (["--bogus", "1"], "unrecognized arguments"),
    ],
)
def test_bad_cli_values(manifest, argv, fragment):
    with pytest.raises(ParamsError) as e:
        cli(manifest, ["--query", "q", *argv])
    assert fragment in str(e.value)


def test_all_problems_reported_together(manifest):
    with pytest.raises(ParamsError) as e:
        cli(manifest, ["--max-pages", "0", "--sort", "cheap"])
    text = str(e.value)
    assert "`query`" in text and "`max_pages`" in text and "`sort`" in text


def test_params_file_then_cli_overrides(manifest, tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"query": "из файла", "max_pages": 7, "tags": ["a"]}), encoding="utf-8")
    p = cli(manifest, ["--params", str(f), "--max-pages", "9"])
    assert (p.query, p.max_pages, p.tags) == ("из файла", 9, ["a"])


def test_params_file_rejects_secrets(manifest, tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"query": "q", "sheet_token": "x"}), encoding="utf-8")
    with pytest.raises(ParamsError) as e:
        cli(manifest, ["--params", str(f)])
    assert "DAWG_SECRET_SHEET_TOKEN" in str(e.value)


def test_params_file_unknown_key(manifest, tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"query": "q", "maxpages": 3}), encoding="utf-8")
    with pytest.raises(ParamsError) as e:
        cli(manifest, ["--params", str(f)])
    assert "`maxpages`: такого параметра нет" in str(e.value)


@pytest.mark.parametrize("content, fragment", [("[1]", "JSON-объект"), ("{", "не JSON")])
def test_params_file_bad_json(manifest, tmp_path, content, fragment):
    f = tmp_path / "p.json"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(ParamsError) as e:
        cli(manifest, ["--params", str(f)])
    assert fragment in str(e.value)


def test_from_app_file(manifest, tmp_path):
    f = tmp_path / "params.json"
    f.write_text(json.dumps({"query": "q", "ratio": 1, "sheet_token": "s3cret"}), encoding="utf-8")
    p = from_file(manifest, f)
    assert p.ratio == 1.0 and isinstance(p.ratio, float)
    assert p.sheet_token == "s3cret"
    assert p.max_pages == 5


def test_from_app_file_invalid(manifest, tmp_path):
    f = tmp_path / "params.json"
    f.write_text(json.dumps({"query": 5}), encoding="utf-8")
    with pytest.raises(ParamsError) as e:
        from_file(manifest, f)
    assert "DAWG_SCRIPT_PARAMS" in str(e.value) and "ожидается строка" in str(e.value)


def test_from_app_file_missing(manifest, tmp_path):
    with pytest.raises(ParamsError, match="не найден"):
        from_file(manifest, tmp_path / "nope.json")


def test_unknown_attribute(manifest):
    p = cli(manifest, ["--query", "q"])
    with pytest.raises(AttributeError, match="max_pages"):
        p.maxpages
    with pytest.raises(KeyError):
        p["maxpages"]
    with pytest.raises(AttributeError):
        p.query = "x"


def test_iteration(manifest):
    p = cli(manifest, ["--query", "q"])
    assert list(p)[:2] == ["query", "max_pages"]
    assert "query" in p and "nope" not in p
    assert len(p) == 10


def test_help_mentions_secret_env(manifest):
    text = build_arg_parser(manifest, "script.py").format_help()
    assert "--max-pages" in text and "DAWG_SECRET_SHEET_TOKEN" in text
    assert "--sheet-token" not in text
