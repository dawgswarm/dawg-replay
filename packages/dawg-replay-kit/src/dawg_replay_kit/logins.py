"""Сохранённые входы: cookies и localStorage браузера между запусками (docs/contract.md, раздел 6.1).

    with kit.browser(login=row["email"]) as b:
        if not b.login_restored or not logged_in(b.page):
            log_in(b.page)

Каталог входов задаёт приложение в `DAWG_SCRIPT_LOGINS_DIR` (`<home>/logins/<script>/`);
в автономном режиме это `./logins/`. Каталог 0700, файл на вход 0600. Имя файла это
хеш имени входа, само имя лежит внутри файла:

    {"v": 1, "name": "user@example.com", "saved_at": "...", "used_at": "...",
     "state": {"cookies": [...], "origins": [{"origin": "...", "localStorage": [...]}]}}

`state` в формате Playwright `storage_state`. Файлы пишутся атомарно (временный файл
и замена), так что прерванная запись не портит прежний вход. Содержимое входа
не попадает в события и логи.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENV_LOGINS_DIR = "DAWG_SCRIPT_LOGINS_DIR"
AUTONOMOUS_DIR = "logins"
FORMAT_VERSION = 1
MAX_NAME = 200
SUFFIX = ".json"

_IS_WINDOWS = sys.platform == "win32"


class LoginError(ValueError):
    """Имя входа не подходит или каталог входов недоступен."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def check_name(name: Any) -> str:
    if isinstance(name, bool) or not isinstance(name, (str, int)):
        raise LoginError("имя входа должно быть строкой, например login=row['email'] или login='кабинет'")
    name = str(name).strip()
    if not name:
        raise LoginError("имя входа пустое")
    if len(name) > MAX_NAME:
        raise LoginError(f"имя входа длиннее {MAX_NAME} символов")
    return name


def file_name(name: str) -> str:
    """Имя файла входа: по нему не видно имени (почты, телефона)."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32] + SUFFIX


@dataclass
class Login:
    name: str
    saved_at: str | None
    used_at: str | None
    path: Path
    state: dict[str, Any]

    def info(self) -> dict[str, Any]:
        """Без содержимого входа: для списков в UI и CLI."""
        return {"name": self.name, "saved_at": self.saved_at, "used_at": self.used_at,
                "cookies": len(self.state.get("cookies") or ()),
                "origins": len(self.state.get("origins") or ())}


class LoginStore:
    """Входы одного скрипта в одном каталоге."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def path(self, name: str) -> Path:
        return self.dir / file_name(check_name(name))

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _IS_WINDOWS:
            os.chmod(self.dir, 0o700)

    def get(self, name: str) -> Login | None:
        return _read(self.path(name))

    def load(self, name: str) -> Login | None:
        """Вход для браузера; отмечает время использования."""
        login = self.get(name)
        if login is not None:
            login.used_at = _now()
            try:
                self._write(login)
            except OSError:
                pass  # время использования не важнее самого входа
        return login

    def save(self, name: str, state: dict[str, Any], previous: Login | None = None) -> Login:
        """Сохраняет состояние браузера. localStorage источников, которые в этот раз
        не открывались, берётся из прежнего входа: иначе он терялся бы."""
        name = check_name(name)
        origins = {o.get("origin"): o for o in (previous.state.get("origins") or ())} if previous else {}
        for o in state.get("origins") or ():
            origins[o.get("origin")] = o
        merged = {"cookies": list(state.get("cookies") or ()), "origins": [o for k, o in origins.items() if k]}
        login = Login(name, _now(), previous.used_at if previous else None, self.path(name), merged)
        self._write(login)
        return login

    def forget(self, name: str) -> bool:
        path = self.path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def forget_all(self) -> int:
        n = 0
        for path in self._files():
            try:
                path.unlink()
                n += 1
            except FileNotFoundError:
                pass
        return n

    def list(self) -> list[Login]:
        out = [login for login in (_read(p) for p in self._files()) if login is not None]
        out.sort(key=lambda x: str(x.name).casefold())
        return out

    def _files(self) -> Iterator[Path]:
        try:
            yield from (p for p in self.dir.iterdir() if p.suffix == SUFFIX and p.is_file())
        except FileNotFoundError:
            return

    def _write(self, login: Login) -> None:
        self._ensure_dir()
        data = {"v": FORMAT_VERSION, "name": login.name, "saved_at": login.saved_at,
                "used_at": login.used_at, "state": login.state}
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=SUFFIX, dir=self.dir)  # 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, login.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        if not _IS_WINDOWS:
            os.chmod(login.path, 0o600)


def _read(path: Path) -> Login | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(data.get("state"), dict):
        return None
    return Login(data["name"], data.get("saved_at"), data.get("used_at"), path, data["state"])


# --- браузер: перенос состояния в контекст Playwright и обратно --------------------------------

_BLANK = "<!doctype html><title></title>"
_SET_ITEMS = "items => { for (const it of items) localStorage.setItem(it.name, it.value) }"


def _origins(state: dict[str, Any]) -> list[tuple[str, list[dict[str, str]]]]:
    out = []
    for o in state.get("origins") or ():
        origin, items = o.get("origin"), o.get("localStorage") or []
        if isinstance(origin, str) and origin.startswith(("http://", "https://")) and items:
            out.append((origin, [{"name": str(i.get("name")), "value": str(i.get("value"))} for i in items]))
    return out


def _fulfill_blank(route: Any) -> None:
    route.fulfill(status=200, content_type="text/html", body=_BLANK)


async def _afulfill_blank(route: Any) -> None:
    await route.fulfill(status=200, content_type="text/html", body=_BLANK)


def restore_sync(context: Any, state: dict[str, Any]) -> None:
    """Дописывает cookies и localStorage в уже открытый контекст DAWG.
    localStorage ставится на пустой странице источника: запросы к сайту перехватываются,
    сеть не трогается."""
    cookies = list(state.get("cookies") or ())
    if cookies:
        context.add_cookies(cookies)
    origins = _origins(state)
    if not origins:
        return
    page = context.new_page()
    try:
        page.route("**/*", _fulfill_blank)
        for origin, items in origins:
            page.goto(origin)
            page.evaluate(_SET_ITEMS, items)
    finally:
        page.close()


async def restore_async(context: Any, state: dict[str, Any]) -> None:
    cookies = list(state.get("cookies") or ())
    if cookies:
        await context.add_cookies(cookies)
    origins = _origins(state)
    if not origins:
        return
    page = await context.new_page()
    try:
        await page.route("**/*", _afulfill_blank)
        for origin, items in origins:
            await page.goto(origin)
            await page.evaluate(_SET_ITEMS, items)
    finally:
        await page.close()


def capture_sync(context: Any) -> dict[str, Any]:
    return dict(context.storage_state())


async def capture_async(context: Any) -> dict[str, Any]:
    return dict(await context.storage_state())


def default_dir(environ: Any, autonomous: bool, cwd: Path) -> Path | None:
    """Каталог входов: из приложения, в автономном режиме `./logins/`. None: приложение
    старое и каталог не передало."""
    if environ.get(ENV_LOGINS_DIR):
        return Path(environ[ENV_LOGINS_DIR])
    return cwd / AUTONOMOUS_DIR if autonomous else None

