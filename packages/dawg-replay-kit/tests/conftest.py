import textwrap

import pytest

from dawg_replay_kit.manifest import parse_manifest

DEMO = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit"]
# ///

# /// dawg-script
# contract = 1
# name = "demo"
# title = "Демо"
# version = 1
#
# [[params]]
# name = "query"
# type = "string"
# title = "Запрос"
# required = true
#
# [[params]]
# name = "max_pages"
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
#
# [[params]]
# name = "start_url"
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
# name = "notes"
# type = "text"
# title = "Заметки"
#
# [[params]]
# name = "sheet_token"
# type = "secret"
# title = "Токен"
# ///
'''


@pytest.fixture
def manifest():
    return parse_manifest(DEMO)


@pytest.fixture
def script_file(tmp_path):
    def write(body: str = "") -> str:
        f = tmp_path / "script.py"
        f.write_text(DEMO + "\n" + textwrap.dedent(body), encoding="utf-8")
        return str(f)

    return write


@pytest.fixture
def demo_text():
    return DEMO
