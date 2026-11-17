"""Окружение запуска (docs/contract.md, раздел 3): откуда брать параметры, куда писать события и файлы."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from dawg_replay_kit import cancel, logins, parameters
from dawg_replay_kit.ask import ENV_ANSWERS_DIR, Asker
from dawg_replay_kit.browser import Browsers
from dawg_replay_kit.dawg_api import DEFAULT_BASE_URL
from dawg_replay_kit.events import ConsoleSink, Emitter, JsonlSink, Sink
from dawg_replay_kit.manifest import Manifest, ManifestError, load_manifest
from dawg_replay_kit.parameters import Params, ParamsError
from dawg_replay_kit.values import Values, default_path

ENV_PARAMS = "DAWG_SCRIPT_PARAMS"
ENV_EVENTS = "DAWG_SCRIPT_EVENTS"
ENV_OUTPUT_DIR = "DAWG_SCRIPT_OUTPUT_DIR"
ENV_RUN_ID = "DAWG_SCRIPT_RUN_ID"
ENV_API_KEY = "DAWG_API_KEY"
ENV_BASE_URL = "DAWG_BASE_URL"

EXIT_BAD_PARAMS = 2


class StartupError(Exception):
    """Скрипт не может стартовать: манифест или параметры не подходят."""


class Kit:
    def __init__(
        self,
        manifest: Manifest,
        params: Params,
        events: Emitter,
        output_dir: Path,
        run_id: str | None,
        autonomous: bool,
        browsers: Browsers | None = None,
        answers_dir: Path | None = None,
        values_path: Path | None = None,
    ):
        self.manifest = manifest
        self.params = params
        self.events = events
        self.output_dir = output_dir
        self.run_id = run_id
        self.autonomous = autonomous
        self.browsers = browsers if browsers is not None else Browsers(events, None, DEFAULT_BASE_URL)
        # Без каталога ответов (автономный режим) вопросы задаются в терминале.
        self.asker = Asker(events, answers_dir)
        self.values = Values(manifest, events, values_path)

    @classmethod
    def from_environment(
        cls,
        script_path: str | Path | None = None,
        argv: Sequence[str] | None = None,
        environ: Mapping[str, str] | None = None,
        stream: TextIO | None = None,
        cwd: Path | None = None,
    ) -> Kit:
        environ = os.environ if environ is None else environ
        argv = sys.argv[1:] if argv is None else argv
        path = Path(script_path) if script_path is not None else _main_script()
        try:
            manifest = load_manifest(path)
        except ManifestError as e:
            raise StartupError(f"манифест скрипта {path} не прошёл проверку:\n{e}") from None

        autonomous = ENV_PARAMS not in environ
        output_dir = Path(environ[ENV_OUTPUT_DIR]) if environ.get(ENV_OUTPUT_DIR) else (cwd or Path.cwd()) / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        sink: Sink
        if environ.get(ENV_EVENTS):
            sink = JsonlSink(environ[ENV_EVENTS])
        else:
            sink = ConsoleSink(stream, output_dir)

        try:
            if autonomous:
                params = parameters.from_cli(manifest, argv, environ, prog=path.name)
            else:
                params = parameters.from_file(manifest, environ[ENV_PARAMS])
        except ParamsError as e:
            # Причину видно и в ленте событий приложения, и в сыром stderr.
            if not isinstance(sink, ConsoleSink):
                failed = Emitter(sink)
                failed.log("error", str(e))
                failed.close()
            else:
                sink.close()
            raise StartupError(str(e)) from None

        api_key = environ.get(ENV_API_KEY) or None
        secrets = params.secret_values() + ([api_key] if api_key else [])
        events = Emitter(sink, secrets=secrets, output_dir=output_dir)
        browsers = Browsers(events, api_key, environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL,
                            logins_dir=logins.default_dir(environ, autonomous, cwd or Path.cwd()))
        answers_dir = None
        if not autonomous:
            if environ.get(ENV_ANSWERS_DIR):
                answers_dir = Path(environ[ENV_ANSWERS_DIR])
            elif environ.get(ENV_EVENTS):
                answers_dir = Path(environ[ENV_EVENTS]).parent / "answers"
        return cls(manifest, params, events, output_dir, environ.get(ENV_RUN_ID) or None, autonomous, browsers,
                   answers_dir, default_path(environ, environ.get(ENV_EVENTS), autonomous))

    def close(self) -> None:
        # Браузеры, которые скрипт не закрыл сам (atexit, отмена), освобождаются до закрытия файла событий.
        self.browsers.close()
        if cancel.received() is not None:
            # Пишется после finally скрипта: отложенные события уже ушли, это последняя строка.
            self.events.log("warn", stop_message(self.autonomous))
        self.events.close()


STOPPED_BY_APP = "запуск остановлен приложением (отмена или таймаут)"
STOPPED_BY_SIGNAL = "запуск остановлен сигналом"


def stop_message(autonomous: bool) -> str:
    """Текст после мягкой остановки. Kit не знает причину: отмену и таймаут приложение
    останавливает одним сигналом, а итог (`cancelled` или `timeout`) ставит само."""
    return STOPPED_BY_SIGNAL if autonomous else STOPPED_BY_APP


def _main_script() -> Path:
    main = sys.modules.get("__main__")
    file = getattr(main, "__file__", None) or (sys.argv[0] if sys.argv and sys.argv[0] else None)
    if not file:
        raise StartupError("не удалось определить файл скрипта: kit работает внутри script.py, запущенного через uv run")
    return Path(file)
