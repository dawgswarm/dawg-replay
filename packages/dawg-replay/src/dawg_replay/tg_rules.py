"""О чём писать в Telegram: общие настройки, переопределения у скрипта и расписания, выбор у запуска.

Уровни, побеждает самый конкретный: запуск (выбор на форме или `--notify`/`--no-notify`,
только для сообщения об успешном итоге), расписание, скрипт, общие настройки (`config.toml`).
У скрипта и расписания каждый ключ либо наследуется, либо задан своим значением.
Переопределения лежат в таблице `telegram_rules` (owner `script:<имя>` или `schedule:<id>`).

Подключён ли Telegram, этот модуль не проверяет: это `dawg_replay.telegram.connection`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from dawg_replay.config import CHOICES, Config, coerce, parse_bool, save_config
from dawg_replay.errors import ReplayError
from dawg_replay.runs import REASON_CANCELLED, Run, parse_ts

if TYPE_CHECKING:
    from dawg_replay.app import App

PREFIX = "telegram_"

# Ключ: (заголовок для настроек, пояснение). Порядок как в настройках.
EVENTS: dict[str, tuple[str, str]] = {
    "failed": ("Запуск упал", "ошибка, таймаут, не стартовал или пропущен по расписанию"),
    "ask": ("Скрипт задал вопрос", "например, просит код из SMS"),
    "done_scheduled": ("Запуск по расписанию закончился", "успешно или частично"),
    "done_manual": ("Ручной запуск закончился", "успешно или частично"),
    "long_minutes": ("Долгий ручной запуск, минут", "порог для «если дольше»"),
    "suspicious": ("Похоже, сайт поменялся", "тихая поломка, строкой в сообщении об итоге"),
    "attach_main": ("Приложить главный файл итога", "документом, до 50 МБ"),
}
KEYS = tuple(EVENTS)
SCHEDULE_KEYS = tuple(k for k in KEYS if k not in ("done_manual", "long_minutes"))
MANUAL = CHOICES[PREFIX + "done_manual"]  # always, long, never
MANUAL_WORDS = {"always": "всегда", "long": "если дольше N минут", "never": "никогда"}
RUN_CHOICES = ("always", "never")  # выбор у запуска; None: по настройкам
INHERIT = "inherit"


def general(cfg: Config) -> dict[str, Any]:
    return {k: getattr(cfg, PREFIX + k) for k in KEYS}


def parse_value(key: str, raw: Any) -> Any:
    """Значение ключа из CLI или формы: on/off, always/long/never, число минут."""
    if key not in KEYS:
        raise ReplayError("unknown_event", f"неизвестное событие `{key}`; есть: {', '.join(KEYS)}")
    full = PREFIX + key
    if key == "done_manual":
        value = str(raw).strip().lower()
        if value not in MANUAL:
            raise ReplayError("bad_config_value", f"`{key}`: одно из {', '.join(MANUAL)}, получено {raw!r}")
        return value
    if key == "long_minutes":
        return coerce(full, raw)
    return parse_bool(key, raw)


def set_general(app: App, key: str, raw: Any) -> dict[str, Any]:
    value = parse_value(key, raw)
    cfg = app.config
    setattr(cfg, PREFIX + key, value)
    save_config(app.paths.config, cfg)
    return general(cfg)


def update_general(app: App, values: dict[str, Any]) -> dict[str, Any]:
    """Меняет несколько ключей разом: при ошибке в любом ничего не сохраняется."""
    parsed = {k: parse_value(k, v) for k, v in values.items()}
    cfg = app.config
    for k, v in parsed.items():
        setattr(cfg, PREFIX + k, v)
    save_config(app.paths.config, cfg)
    return general(cfg)


# --- переопределения ---------------------------------------------------------------


def _owner(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def overrides(app: App, kind: str, name: str) -> dict[str, Any]:
    row = app.db.execute("SELECT rules_json FROM telegram_rules WHERE owner = ?", (_owner(kind, name),)).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row[0])
    except ValueError:
        return {}
    allowed = KEYS if kind == "script" else SCHEDULE_KEYS
    out = {}
    for k, v in data.items() if isinstance(data, dict) else ():
        if k in allowed:
            try:
                out[k] = parse_value(k, v)
            except ReplayError:
                continue
    return out


def set_overrides(app: App, kind: str, name: str, values: dict[str, Any]) -> dict[str, Any]:
    """Задаёт переопределения поключево: значение INHERIT (или None) снимает ключ. Остальные ключи не трогает."""
    if kind not in ("script", "schedule"):
        raise ValueError(kind)
    allowed = KEYS if kind == "script" else SCHEDULE_KEYS
    current = overrides(app, kind, name)
    for k, raw in values.items():
        if k not in allowed:
            if k in KEYS:
                raise ReplayError("bad_event", f"`{k}` у расписания не задаётся: оно запускает скрипт не вручную")
            parse_value(k, raw)  # неизвестный ключ: понятная ошибка
        if raw is None or raw == INHERIT:
            current.pop(k, None)
        else:
            current[k] = parse_value(k, raw)
    if current:
        app.db.execute(
            "INSERT INTO telegram_rules (owner, rules_json) VALUES (?, ?)"
            " ON CONFLICT(owner) DO UPDATE SET rules_json = excluded.rules_json",
            (_owner(kind, name), json.dumps(current)),
        )
    else:
        drop(app, kind, name)
    return current


def drop(app: App, kind: str, name: str) -> None:
    app.db.execute("DELETE FROM telegram_rules WHERE owner = ?", (_owner(kind, name),))


def effective(app: App, script: str, schedule_id: str | None = None) -> dict[str, Any]:
    """Итоговые правила для скрипта (и расписания): общие, поверх скрипт, поверх расписание."""
    out = general(app.config)
    out.update(overrides(app, "script", script))
    if schedule_id:
        out.update(overrides(app, "schedule", schedule_id))
    return out


def layers(app: App, kind: str, name: str, script: str | None = None) -> list[dict[str, Any]]:
    """Строки для блока на странице скрипта или у расписания: ключ, своё значение или None, что наследуется."""
    if kind == "script":
        inherited, own, keys = general(app.config), overrides(app, "script", name), KEYS
    else:
        inherited = effective(app, script or "")
        own, keys = overrides(app, "schedule", name), SCHEDULE_KEYS
    return [
        {"key": k, "title": EVENTS[k][0], "hint": EVENTS[k][1], "own": own.get(k), "inherited": inherited[k],
         "value": own.get(k, inherited[k])}
        for k in keys
    ]


def wants_ask(app: App, rec: Run) -> bool:
    """Писать ли о вопросе скрипта. Черновые запуски не пишут ничего."""
    if rec.draft:
        return False
    return bool(effective(app, rec.script_name, rec.schedule_id)["ask"])


def manual_text(rules: dict[str, Any]) -> str:
    """Что значит «по настройкам» для ручного запуска сейчас: подсказка на форме."""
    mode = rules["done_manual"]
    if mode == "always":
        return "всегда"
    if mode == "never":
        return "только если упадёт" if rules["failed"] else "не сообщать"
    return f"если дольше {rules['long_minutes']} мин"


# --- что слать об итоге ------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    kind: str | None  # failed, done или None (ничего)
    suspicion: dict[str, Any] | None = None  # строка о тихой поломке в сообщении об итоге
    attach_main: bool = False

    @property
    def send(self) -> bool:
        return self.kind is not None


NOTHING = Plan(None)


def duration_seconds(rec: Run) -> float | None:
    start, end = parse_ts(rec.started_at), parse_ts(rec.finished_at)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def plan(app: App, rec: Run, suspicion: dict[str, Any] | None = None) -> Plan:
    """Нужно ли сообщение об итоге запуска и какое. `suspicion`: тихая поломка (repair.suspicion), если есть."""
    if rec.draft or rec.status not in ("ok", "partial", "failed"):
        return NOTHING
    rules = effective(app, rec.script_name, rec.schedule_id)
    if rec.status == "failed":
        if rec.reason == REASON_CANCELLED:
            return NOTHING  # отмену пользователь сделал сам, остановку сервера UI тоже
        return Plan("failed") if rules["failed"] else NOTHING
    if rec.notify == "never":
        return NOTHING
    if rec.notify == "always":
        done = True
    elif rec.trigger in ("schedule", "link"):  # запуск по связи для правил считается плановым
        done = bool(rules["done_scheduled"])
    elif rules["done_manual"] == "always":
        done = True
    elif rules["done_manual"] == "long":
        took = duration_seconds(rec)
        done = took is not None and took >= rules["long_minutes"] * 60
    else:
        done = False
    sus = suspicion if rules["suspicious"] else None
    if not done and sus is None:
        return NOTHING
    return Plan("done", sus, bool(rules["attach_main"]))


def started_local(rec: Run) -> datetime | None:
    ts = parse_ts(rec.started_at or rec.created_at)
    return ts.astimezone() if ts else None


# --- форма для экранов --------------------------------------------------------------


MANUAL_CHOICES = (("always", "всегда"), ("long", "если дольше порога"), ("never", "никогда"))


def value_text(key: str, value: Any) -> str:
    if key == "done_manual":
        return dict(MANUAL_CHOICES)[value]
    if key == "long_minutes":
        return f"{value} мин"
    return "писать" if value else "не писать"


def connected(app: App) -> bool:
    """Чат привязан. Токен не читается: блоки правил показываются без обращения к хранилищу паролей."""
    return bool(app.config.telegram_chat_id)


def form_ctx(app: App, scope: str, name: str | None = None, script: str | None = None, **extra: Any) -> dict[str, Any]:
    """Контекст шаблона _telegram_rules.html."""
    from urllib.parse import quote

    if scope == "general":
        rules = general(app.config)
        rows = [{"key": k, "title": EVENTS[k][0], "hint": EVENTS[k][1], "value": rules[k]} for k in KEYS]
        action, dom_id, word = "/settings/telegram/rules", "tg-rules", ""
    elif scope == "script":
        rows = layers(app, "script", name or "")
        action, dom_id, word = f"/scripts/{quote(name or '')}/telegram", "tg-rules-script", "как в общих"
    else:
        rows = layers(app, "schedule", name or "", script)
        action = f"/scripts/{quote(script or '')}/schedules/{quote(name or '')}/telegram"
        dom_id, word = f"tg-rules-{name}", "как у скрипта"
    for r in rows:
        if "inherited" in r:
            r["inherited_text"] = value_text(r["key"], r["inherited"])
    own = sum(1 for r in rows if r.get("own") is not None)
    return {"scope": scope, "rows": rows, "action": action, "dom_id": dom_id, "inherit_word": word,
            "manual": MANUAL_CHOICES, "own_count": own, **extra}


def parse_form(scope: str, form: Any) -> dict[str, Any]:
    """Значения из формы. В общих настройках галочка без отметки значит off."""
    if scope == "general":
        out: dict[str, Any] = {}
        for k in KEYS:
            if k in ("done_manual", "long_minutes"):
                out[k] = form.get(k, "")
            else:
                out[k] = "on" if form.get(k) else "off"
        return out
    keys = KEYS if scope == "script" else SCHEDULE_KEYS
    out = {}
    for k in keys:
        raw = str(form.get(k, "") or "").strip()
        out[k] = INHERIT if raw in ("", INHERIT) else raw
    return out
