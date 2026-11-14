"""Фейковый schtasks для тестов: задачи в JSON-файле FAKE_SCHTASKS_FILE. Планировщик Windows не трогается."""

import json
import os
import sys
from pathlib import Path

path = Path(os.environ["FAKE_SCHTASKS_FILE"])
tasks = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


if args[:1] == ["/Query"]:
    for name in tasks:
        print(f'"\\{name}","N/A","Ready"')
elif args[:1] == ["/Create"]:
    tasks[opt("/TN")] = {"tr": opt("/TR"), "args": args}
    path.write_text(json.dumps(tasks), encoding="utf-8")
    print("SUCCESS: The scheduled task has successfully been created.")
elif args[:1] == ["/Delete"]:
    name = opt("/TN")
    if name not in tasks:
        print("ERROR: The system cannot find the file specified.", file=sys.stderr)
        sys.exit(1)
    del tasks[name]
    path.write_text(json.dumps(tasks), encoding="utf-8")
else:
    print(f"ERROR: bad args {args}", file=sys.stderr)
    sys.exit(1)
