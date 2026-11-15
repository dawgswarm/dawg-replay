"""Ключ DAWG и секреты скриптов в системном keyring. В БД и файлах их нет."""

from __future__ import annotations

import re
from typing import Protocol

from dawg_replay.errors import ReplayError

SERVICE = "dawg-replay"
API_KEY_USER = "dawg-api-key"
TELEGRAM_USER = "telegram-bot-token"


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
        self._b.set_password(SERVICE, API_KEY_USER, clean_secret(value, "ключ DAWG"))

    def get_api_key(self) -> str | None:
        """Сохранённый ключ. С управляющими символами (старая порча при вставке) `corrupt_api_key`:
        платформа отбрасывает такой заголовок кодом 400 без объяснений."""
        key = self.api_key_raw()
        if key is not None and has_control_chars(key):
            raise ReplayError("corrupt_api_key", CORRUPT_API_KEY)
        return key

    def api_key_state(self) -> str:
        """`missing`, `set` или `corrupt`; сам ключ наружу не отдаёт."""
        key = self.api_key_raw()
        if key is None:
            return "missing"
        return "corrupt" if has_control_chars(key) else "set"

    def api_key_raw(self) -> str | None:
        return self._b.get_password(SERVICE, API_KEY_USER) or None

    def set_telegram_token(self, value: str) -> None:
        self._b.set_password(SERVICE, TELEGRAM_USER, clean_secret(value, "токен бота", command=None))

    def get_telegram_token(self) -> str | None:
        return self._b.get_password(SERVICE, TELEGRAM_USER) or None

    def delete_telegram_token(self) -> None:
        self._b.delete_password(SERVICE, TELEGRAM_USER)

    def set_secret(self, script: str, param: str, value: str) -> None:
        self._b.set_password(SERVICE, _secret_user(script, param), clean_secret(value, "секрет", f"dawg-replay secret set {script} {param}"))

    def get_secret(self, script: str, param: str) -> str | None:
        return self._b.get_password(SERVICE, _secret_user(script, param)) or None

    def delete_secret(self, script: str, param: str) -> None:
        self._b.delete_password(SERVICE, _secret_user(script, param))


CORRUPT_API_KEY = "ключ DAWG повреждён, задайте заново: dawg-replay config set-key"

# Маркеры bracketed paste: терминал оборачивает ими вставленный текст, getpass их не убирает.
_PASTE_MARKERS = ("\x1b[200~", "\x1b[201~")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def has_control_chars(value: str) -> bool:
    return _CONTROL.search(value) is not None


def clean_secret(value: str, what: str, command: str | None = "dawg-replay config set-key") -> str:
    """Убирает маркеры вставки и пробелы по краям. Пустое значение и значение с управляющими
    символами не сохраняются; в тексте ошибки самого значения нет."""
    for marker in _PASTE_MARKERS:
        value = value.replace(marker, "")
    value = value.strip()
    if not value:
        raise ReplayError("empty_secret", f"{what} пустой")
    if has_control_chars(value):
        msg = f"{what} содержит невидимые управляющие символы и не сохранён."
        if command:
            msg += (
                " Так бывает при вставке в терминал; передайте значение через stdin: "
                f"pbpaste | {command} (macOS) или {command} < файл"
            )
        raise ReplayError("bad_secret", msg)
    return value
