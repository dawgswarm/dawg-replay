"""Выходные значения скрипта (контракт 1.5): `kit.output("token", value)`.

Значение объявляется в манифесте:

    [[outputs]]
    kind = "value"
    name = "token"
    type = "string"
    secret = true

В режиме приложения отданные значения пишутся файлом JSON `{имя: значение}` по пути из
`DAWG_SCRIPT_VALUES` (по умолчанию `values.json` рядом с файлом событий); приложение забирает
их после завершения. В журнал событий идёт событие `output`: имя, подпись и само значение,
у секретного значения только пометка `secret`. В автономном режиме значения видны в консоли.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from dawg_replay_kit.events import Emitter
from dawg_replay_kit.manifest import Manifest

ENV_VALUES = "DAWG_SCRIPT_VALUES"
VALUES_FILE = "values.json"


def _plain(value: Any, t: str | None) -> Any:
    """Даты и время из Python в строку, как в параметрах; кортеж и множество в список."""
    if isinstance(value, datetime) and t == "datetime":
        return value.strftime("%Y-%m-%dT%H:%M")
    if isinstance(value, date) and not isinstance(value, datetime) and t == "date":
        return value.isoformat()
    if isinstance(value, time) and t == "time":
        return value.strftime("%H:%M")
    if isinstance(value, (tuple, set, frozenset)) and t in ("url_list", "string_list", "multi_enum"):
        return list(value)
    return value


class Values:
    def __init__(self, manifest: Manifest, events: Emitter, path: Path | None):
        self._manifest = manifest
        self._events = events
        self._path = path
        self._values: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def given(self) -> dict[str, Any]:
        return dict(self._values)

    def output(self, name: str, value: Any) -> None:
        try:
            out = self._manifest.value_out(name)
        except KeyError:
            declared = ", ".join(o.name for o in self._manifest.values_out()) or "нет"
            raise ValueError(f"kit.output: значение `{name}` не объявлено в [[outputs]] с kind = \"value\"; "
                             f"объявлены: {declared}") from None
        value = _plain(value, out.type)
        problem = out.check(value)
        if problem:
            raise ValueError(f"kit.output: значение `{name}` ({out.type}): {problem}")
        if out.secret:
            for s in (value if isinstance(value, list) else [value]):
                self._events.add_secret(str(s))
        with self._lock:
            self._values[name] = value
            self._write()
        if out.secret:
            self._events.emit("output", name=name, title=out.title, secret=True)
        else:
            self._events.emit("output", name=name, title=out.title, value=value)

    def _write(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # секретные значения: только владельцу
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._values, f, ensure_ascii=False)
        os.replace(tmp, self._path)


def default_path(environ: Any, events_path: str | None, autonomous: bool) -> Path | None:
    if environ.get(ENV_VALUES):
        return Path(environ[ENV_VALUES])
    if autonomous or not events_path:
        return None
    return Path(events_path).parent / VALUES_FILE
