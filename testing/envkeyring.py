"""Бэкенд keyring для тестов в отдельных процессах: ключ DAWG из DAWG_API_KEY, остальное в памяти.

    PYTHON_KEYRING_BACKEND=envkeyring.EnvKeyring, PYTHONPATH с каталогом testing.
"""

import os

from keyring.backend import KeyringBackend


class EnvKeyring(KeyringBackend):
    priority = 1
    _mem: dict = {}

    def get_password(self, service, username):
        if (service, username) == ("dawg-replay", "dawg-api-key"):
            return os.environ.get("DAWG_API_KEY")
        return self._mem.get((service, username))

    def set_password(self, service, username, password):
        self._mem[(service, username)] = password

    def delete_password(self, service, username):
        self._mem.pop((service, username), None)
