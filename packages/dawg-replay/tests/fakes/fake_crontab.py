"""Фейковый crontab для тестов: `-l` и `-` над файлом FAKE_CRONTAB_FILE. Настоящий crontab не трогается."""

import os
import sys
from pathlib import Path

path = Path(os.environ["FAKE_CRONTAB_FILE"])
if os.environ.get("FAKE_CRONTAB_FAIL"):
    print("crontab: permission denied", file=sys.stderr)
    sys.exit(1)
if sys.argv[1:] == ["-l"]:
    if not path.exists():
        print("crontab: no crontab for tester", file=sys.stderr)
        sys.exit(1)
    sys.stdout.buffer.write(path.read_bytes())
elif sys.argv[1:] == ["-"]:
    path.write_bytes(sys.stdin.buffer.read())
else:
    print(f"usage: crontab [-l | -], got {sys.argv[1:]}", file=sys.stderr)
    sys.exit(2)
