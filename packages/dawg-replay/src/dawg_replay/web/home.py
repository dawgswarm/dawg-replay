"""Данные для главной (дашборд) и библиотеки скриптов. Только чтение ядра.

Сайт и метки скрипта берутся из манифеста (`site`, `tags`), если они там есть.
Без сайта буква и цвет обложки считаются от названия, цвет одинаковый при
каждом показе.
"""

from __future__ import annotations

import hashlib
from typing import Any

from dawg_replay import diagnosis, insurance
from dawg_replay.errors import ReplayError

HUES = ("teal", "amber", "indigo", "rose", "sky", "olive")
ATTENTION_LABELS = ("failed", "partial", "failed (timeout)", "failed (lost)")
RECENT_RUNS = 8
QUICK_PRESETS = 6
SUSPECT_SCAN = 30  # сколько последних завершённых запусков смотреть на остаточные браузеры


def hue_for(key: str) -> str:
    digest = hashlib.sha1(key.casefold().encode("utf-8")).digest()
    return HUES[digest[0] % len(HUES)]


def letter_for(text: str) -> str:
    for ch in text:
        if ch.isalnum():
            return ch.upper()
    return "?"


def manifest_meta(app: Any, name: str) -> dict[str, Any]:
    """site и tags из манифеста; у старых манифестов их нет."""
    try:
        m = app.registry.manifest(name)
    except ReplayError:
        return {"site": None, "tags": ()}
    site = getattr(m, "site", None) or None
    tags = tuple(getattr(m, "tags", ()) or ())
    return {"site": site, "tags": tags}


def cover(title: str, site: str | None) -> dict[str, str]:
    key = site or title
    return {"letter": letter_for(key), "hue": hue_for(key), "site": site or ""}


def library(app: Any, query: str = "", site: str = "") -> dict[str, Any]:
    """Карточки библиотеки, список сайтов для фильтра. Фильтры: подстрока названия и сайт."""
    items = []
    for row in app.scripts_overview():
        s = row["script"]
        meta = manifest_meta(app, s["name"])
        try:
            presets = [p.name for p in app.list_presets(s["name"])]
        except ReplayError:
            presets = []
        items.append({
            "script": s,
            "last_run": row["last_run"],
            "presets": presets,
            "tags": list(meta["tags"]),
            "cover": cover(s["title"], meta["site"]),
        })
    sites = sorted({i["cover"]["site"] for i in items if i["cover"]["site"]}, key=str.casefold)
    q = query.strip().casefold()
    shown = [
        i for i in items
        if (not q or q in i["script"]["title"].casefold() or q in i["script"]["name"].casefold()
            or q in (i["script"].get("description") or "").casefold())
        and (not site or i["cover"]["site"] == site)
    ]
    return {"cards": shown, "total": len(items), "sites": sites, "query": query.strip(), "site": site}


def _title(app: Any, rec: Any, cache: dict[str, tuple[str, dict[str, str]]]) -> tuple[str, dict[str, str]]:
    if rec.script_name not in cache:
        title = rec.script_name
        try:
            title = app.registry.get(rec.script_name).title
        except ReplayError:
            pass
        cache[rec.script_name] = (title, cover(title, manifest_meta(app, rec.script_name)["site"]))
    return cache[rec.script_name]


def _progress(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    for p in (snapshot.get("progress") or {}).values():
        if p.get("total"):
            return p
    return None


def dashboard(app: Any, views: Any) -> dict[str, Any]:
    """Всё для главной, кроме минут DAWG (они грузятся отдельным запросом)."""
    titles: dict[str, tuple[str, dict[str, str]]] = {}

    running = []
    for rec in app.runs.running():
        title, cov = _title(app, rec, titles)
        step = progress = None
        try:
            snap = views.get(rec.id).snapshot()
            timeline = snap.get("timeline") or []
            step = timeline[-1]["name"] if timeline else None
            progress = _progress(snap)
        except (ReplayError, OSError):
            pass
        running.append({"run": rec.to_dict(), "title": title, "cover": cov, "step": step, "progress": progress,
                        "cancelling": rec.cancel_requested_at is not None})

    recent_recs = app.runs.list(None, max(RECENT_RUNS, SUSPECT_SCAN))
    recent = []
    for rec in recent_recs[:RECENT_RUNS]:
        if rec.running:
            continue
        title, cov = _title(app, rec, titles)
        recent.append({"run": rec.to_dict(), "title": title, "cover": cov})

    # Требует внимания: последний запуск скрипта упал или собрал не всё.
    attention, seen = [], set()
    for rec in recent_recs:
        if rec.draft or rec.script_name in seen or rec.running:
            continue
        seen.add(rec.script_name)
        if rec.label in ATTENTION_LABELS:
            title, cov = _title(app, rec, titles)
            why = None
            if not rec.summary and not rec.error:
                dg = diagnosis.diagnose(app, rec, lines=10)
                why = dg["summary"] if dg else None
            attention.append({"run": rec.to_dict(), "title": title, "cover": cov, "why": why})

    suspects = []
    for rec in recent_recs:
        if rec.running:
            continue
        pending = insurance.pending_suspects(app.paths.run_dir(rec.id))
        if pending:
            title, _ = _title(app, rec, titles)
            suspects.append({"run": rec.to_dict(), "title": title, "suspects": pending})

    return {"running": running, "attention": attention[:5], "recent": recent, "suspects": suspects,
            "quick": quick_presets(app, recent_recs, titles)}


def quick_presets(app: Any, recs: list[Any], titles: dict[str, tuple[str, dict[str, str]]]) -> list[dict[str, Any]]:
    """Недавно использованные пресеты, затем пресеты скриптов из недавних запусков."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    existing: dict[str, list[str]] = {}

    def presets_of(name: str) -> list[str]:
        if name not in existing:
            try:
                existing[name] = [p.name for p in app.list_presets(name)]
            except ReplayError:
                existing[name] = []
        return existing[name]

    def add(rec: Any, preset: str) -> None:
        key = (rec.script_name, preset)
        if key in seen or preset not in presets_of(rec.script_name):
            return
        seen.add(key)
        title, cov = _title(app, rec, titles)
        out.append({"script": rec.script_name, "preset": preset, "title": title, "cover": cov})

    for rec in recs:
        if rec.preset and not rec.draft:
            add(rec, rec.preset)
    for rec in recs:
        if rec.draft:
            continue
        for p in presets_of(rec.script_name):
            add(rec, p)
    return out[:QUICK_PRESETS]
