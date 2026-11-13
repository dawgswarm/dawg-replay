import json

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from dawg_replay.app import App
from dawg_replay.cli import main


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError(username)
        del self.store[(service, username)]


def script_text(version=1, name="ozon-cards", extra_params="", title="Ozon: карточки"):
    return f"""\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1,<2"]
# ///

# /// dawg-script
# contract = 1
# name = "{name}"
# title = "{title}"
# version = {version}
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
# name = "sheet_token"
# type = "secret"
# title = "Токен"
{extra_params}# ///

print("hi")
"""


@pytest.fixture
def kr(monkeypatch):
    backend = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(old)


@pytest.fixture
def home(tmp_path, monkeypatch, kr):
    h = tmp_path / "home"
    monkeypatch.setenv("DAWG_REPLAY_HOME", str(h))
    return h


@pytest.fixture
def app(home, kr):
    with App(home, keyring_backend=kr) as a:
        yield a


@pytest.fixture
def write_script(tmp_path):
    def write(text=None, filename="script.py", **kw):
        f = tmp_path / "src" / filename
        f.parent.mkdir(exist_ok=True)
        f.write_text(text if text is not None else script_text(**kw), encoding="utf-8")
        return f

    return write


@pytest.fixture
def cli(home, capsys, monkeypatch):
    """Запускает CLI с --json, возвращает (код выхода, разобранный JSON)."""

    def run(*argv, stdin=None):
        if stdin is not None:
            import io

            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = main(["--json", *argv])
        out = capsys.readouterr().out
        return code, json.loads(out)

    return run
