"""Бэкенд keyring в файле для проверок в нескольких процессах: системное хранилище паролей не трогается.

    PYTHON_KEYRING_BACKEND=filekeyring.FileKeyring, FILEKEYRING_PATH=<файл>, PYTHONPATH с каталогом testing.

Файл создаётся с правами 0600. Только для тестов и проверок: значения лежат открытым текстом.
"""

import json
import os

from keyring.backend import KeyringBackend


class FileKeyring(KeyringBackend):
    priority = 1

    def _path(self) -> str:
        return os.environ["FILEKEYRING_PATH"]

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _dump(self, data: dict) -> None:
        tmp = f"{self._path()}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self._path())

    def get_password(self, service, username):
        return self._load().get(f"{service}|{username}")

    def set_password(self, service, username, password):
        data = self._load()
        data[f"{service}|{username}"] = password
        self._dump(data)

    def delete_password(self, service, username):
        data = self._load()
        data.pop(f"{service}|{username}", None)
        self._dump(data)
