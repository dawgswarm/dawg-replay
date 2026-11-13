"""Окружение запуска (docs/contract.md, раздел 3): откуда брать параметры, куда писать события и файлы."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from dawg_replay_kit import parameters
from dawg_replay_kit.events import ConsoleSink, Emitter, JsonlSink, Sink
from dawg_replay_kit.manifest import Manifest, ManifestError, load_manifest
from dawg_replay_kit.parameters import Params, ParamsError

ENV_PARAMS = "DAWG_SCRIPT_PARAMS"
ENV_EVENTS = "DAWG_SCRIPT_EVENTS"
ENV_OUTPUT_DIR = "DAWG_SCRIPT_OUTPUT_DIR"
ENV_RUN_ID = "DAWG_SCRIPT_RUN_ID"

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
    ):
        self.manifest = manifest
        self.params = params
        self.events = events
        self.output_dir = output_dir
        self.run_id = run_id
        self.autonomous = autonomous

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

        events = Emitter(sink, secrets=params.secret_values(), output_dir=output_dir)
        return cls(manifest, params, events, output_dir, environ.get(ENV_RUN_ID) or None, autonomous)

    def close(self) -> None:
        self.events.close()


def _main_script() -> Path:
    main = sys.modules.get("__main__")
    file = getattr(main, "__file__", None) or (sys.argv[0] if sys.argv and sys.argv[0] else None)
    if not file:
        raise StartupError("не удалось определить файл скрипта: kit работает внутри script.py, запущенного через uv run")
    return Path(file)
