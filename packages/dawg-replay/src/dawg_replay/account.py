"""Аккаунт DAWG: проверка ключа и остаток минут через GET /api/v1/usage (того же запроса нет в SDK)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dawg_replay_kit.dawg_api import DEFAULT_BASE_URL

from dawg_replay.errors import ReplayError

TIMEOUT = 15.0


@dataclass(frozen=True)
class Usage:
    plan: str | None
    used_minutes: float | None
    included_minutes: float | None
    remaining_minutes: float | None
    active_browsers: int | None
    max_browsers: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "used_minutes": self.used_minutes,
            "included_minutes": self.included_minutes,
            "remaining_minutes": self.remaining_minutes,
            "active_browsers": self.active_browsers,
            "max_browsers": self.max_browsers,
        }


def fetch_usage(api_key: str, base_url: str | None = None) -> Usage:
    """Запрашивает usage. Неверный ключ: ReplayError `bad_api_key`, сеть или сервер: `dawg_unavailable`."""
    import httpx

    url = f"{(base_url or DEFAULT_BASE_URL).rstrip('/')}/api/v1/usage"
    try:
        resp = httpx.get(url, headers={"X-API-Key": api_key}, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise ReplayError("dawg_unavailable", f"платформа DAWG недоступна: {e}") from None
    if resp.status_code in (401, 403):
        raise ReplayError("bad_api_key", "ключ DAWG не подходит: платформа его не приняла")
    if resp.status_code >= 400:
        raise ReplayError("dawg_unavailable", f"платформа DAWG ответила ошибкой HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise ReplayError("dawg_unavailable", "платформа DAWG вернула не JSON") from None
    d = body.get("data", body) if isinstance(body, dict) else {}
    return Usage(
        plan=d.get("plan"),
        used_minutes=d.get("usedMinutes"),
        included_minutes=d.get("includedMinutes"),
        remaining_minutes=d.get("remainingMinutes"),
        active_browsers=d.get("activeBrowsers"),
        max_browsers=d.get("maxBrowsers"),
    )


def key_hint(key: str | None) -> str | None:
    """Ключ для показа: только последние 4 символа."""
    if not key:
        return None
    return "••••" + key[-4:] if len(key) > 8 else "••••"
