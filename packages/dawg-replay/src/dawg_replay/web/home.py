"""Данные для главной (дашборд) и библиотеки скриптов. Только чтение ядра.

Сайт и метки скрипта берутся из манифеста (`site`, `tags`), если они там есть.
Без сайта буква и цвет обложки считаются от названия, цвет одинаковый при
каждом показе.
"""

from __future__ import annotations

import hashlib
from typing import Any

from dawg_replay import diagnosis, insurance, repair
from dawg_replay.errors import ReplayError

HUES = ("teal", "amber", "indigo", "rose", "sky", "olive")
ATTENTION_LABELS = ("failed", "partial", "failed (timeout)", "failed (lost)")
RECENT_RUNS = 8
QUICK_PRESETS = 6
SUSPECT_SCAN = 30  # сколько последних завершённых запусков смотреть на остаточные браузеры
SINCE_SCAN = 100  # сколько последних запусков смотреть для блока «С прошлого визита»
SINCE_SHOWN = 8


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


def dashboard(app: Any, views: Any, since: str | None = None) -> dict[str, Any]:
    """Всё для главной, кроме минут DAWG (они грузятся отдельным запросом).
    `since`: граница блока «С прошлого визита» (dawg_replay.uistate)."""
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
        sus = repair.suspicion(app, rec)
        if rec.label in ATTENTION_LABELS or sus:
            title, cov = _title(app, rec, titles)
            attention.append({"run": rec.to_dict(), "title": title, "cover": cov, "why": _why(app, rec),
                              "suspicion": sus})

    suspects = []
    for rec in recent_recs:
        if rec.running:
            continue
        pending = insurance.pending_suspects(app.paths.run_dir(rec.id))
        if pending:
            title, _ = _title(app, rec, titles)
            suspects.append({"run": rec.to_dict(), "title": title, "suspects": pending})

    return {"running": running, "attention": attention[:5], "recent": recent, "suspects": suspects,
            "quick": quick_presets(app, recent_recs, titles), "since": since_last_visit(app, since, titles)}


def _why(app: Any, rec: Any) -> str | None:
    if rec.summary or rec.error:
        return None
    dg = diagnosis.diagnose(app, rec, lines=10)
    return dg["summary"] if dg else None


def since_last_visit(app: Any, since: str | None, titles: dict[str, tuple[str, dict[str, str]]]) -> dict[str, Any] | None:
    """Упавшие (не по отмене) и подозрительные запуски, закончившиеся после прошлого визита.
    None, если прошлого визита не было."""
    if since is None:
        return None
    items: list[dict[str, Any]] = []
    total = 0
    for rec in app.runs.list(None, SINCE_SCAN):
        if rec.running or rec.draft or not rec.finished_at or rec.finished_at <= since:
            continue
        need = repair.needs_repair(app, rec)
        if need is None:
            continue
        total += 1
        if len(items) < SINCE_SHOWN:
            title, cov = _title(app, rec, titles)
            items.append({"run": rec.to_dict(), "title": title, "cover": cov, "why": _why(app, rec),
                          "suspicion": need["suspicion"]})
    return {"since": since, "items": items, "total": total}


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
