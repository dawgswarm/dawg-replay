"""Ключ DAWG и секреты скриптов в системном keyring. В БД и файлах их нет."""

from __future__ import annotations

from typing import Protocol

from dawg_replay.errors import ReplayError

SERVICE = "dawg-replay"
API_KEY_USER = "dawg-api-key"


class Backend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class _KeyringModule:
    """Обёртка над модулем keyring: импорт при первом обращении и понятные ошибки."""

    def _kr(self):
        import keyring

        return keyring

    def get_password(self, service: str, username: str) -> str | None:
        return self._call("get_password", service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        self._call("set_password", service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        kr = self._kr()
        try:
            kr.delete_password(service, username)
        except kr.errors.PasswordDeleteError:
            pass
        except kr.errors.KeyringError as e:
            raise _unavailable(e) from None

    def _call(self, method: str, *args):
        kr = self._kr()
        try:
            return getattr(kr, method)(*args)
        except kr.errors.KeyringError as e:
            raise _unavailable(e) from None


def _unavailable(e: Exception) -> ReplayError:
    return ReplayError(
        "keyring_unavailable",
        f"системное хранилище паролей недоступно: {e}. "
        "На Linux без графической сессии нужен Secret Service или другой бэкенд keyring",
    )


def _secret_user(script: str, param: str) -> str:
    return f"secret:{script}:{param}"


class SecretStore:
    def __init__(self, backend: Backend | None = None):
        self._b = backend if backend is not None else _KeyringModule()

    def set_api_key(self, value: str) -> None:
        self._b.set_password(SERVICE, API_KEY_USER, _non_empty(value, "ключ DAWG"))

    def get_api_key(self) -> str | None:
        return self._b.get_password(SERVICE, API_KEY_USER) or None

    def set_secret(self, script: str, param: str, value: str) -> None:
        self._b.set_password(SERVICE, _secret_user(script, param), _non_empty(value, "секрет"))

    def get_secret(self, script: str, param: str) -> str | None:
        return self._b.get_password(SERVICE, _secret_user(script, param)) or None

    def delete_secret(self, script: str, param: str) -> None:
        self._b.delete_password(SERVICE, _secret_user(script, param))


def _non_empty(value: str, what: str) -> str:
    value = value.strip()
    if not value:
        raise ReplayError("empty_secret", f"{what} пустой")
    return value
