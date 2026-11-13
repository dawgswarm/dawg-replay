"""Время старта процесса и время загрузки системы.

Нужны, чтобы отличать свой процесс от чужого с тем же pid: pid переиспользуется,
особенно после перезагрузки. Приложение запоминает время старта супервизора
и скрипта, полученное отсюда же, и перед проверкой «жив ли» и перед kill
сверяет его с текущим временем старта процесса с этим pid.

Источник: Linux `/proc`, macOS и другие POSIX `ps -o lstart` (точность секунда),
Windows `GetProcessTimes`. Время сравнивается с допуском `TOLERANCE`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import datetime

TOLERANCE = 2.0  # секунды: ps даёт целые секунды, /proc считает от целой btime

_IS_WINDOWS = sys.platform == "win32"


def start_time(pid: int) -> float | None:
    """Время старта процесса (Unix time) или None, если процесса нет или узнать не вышло."""
    if pid <= 0:
        return None
    try:
        if _IS_WINDOWS:
            return _win_start_time(pid)
        if os.path.exists(f"/proc/{pid}/stat"):
            return _proc_start_time(pid)
        return _ps_start_time(pid)
    except (OSError, ValueError):
        return None


def boot_time() -> float | None:
    """Время последней загрузки системы (Unix time) или None."""
    try:
        if _IS_WINDOWS:
            import ctypes

            return time.time() - ctypes.windll.kernel32.GetTickCount64() / 1000.0  # type: ignore[attr-defined]
        if os.path.exists("/proc/stat"):
            with open("/proc/stat", encoding="ascii") as f:
                for line in f:
                    if line.startswith("btime "):
                        return float(line.split()[1])
            return None
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        m = re.search(r"sec = (\d+)", out)
        return float(m.group(1)) if m else None
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def same_process(pid: int | None, expected_start: float | None) -> bool:
    """Процесс `pid` жив и стартовал тогда же, когда записанный. Без записи времени — False."""
    if not pid or expected_start is None:
        return False
    actual = start_time(pid)
    return actual is not None and abs(actual - expected_start) <= TOLERANCE


# --- источники -----------------------------------------------------------------


def _proc_start_time(pid: int) -> float | None:
    with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
        stat = f.read()
    # Имя процесса в скобках может содержать пробелы и скобки: поля идут после последней `)`.
    fields = stat[stat.rindex(")") + 2 :].split()
    ticks = int(fields[19])  # поле 22 starttime, отсчёт с поля 3
    if fields[0] == "Z":
        return None
    btime = boot_time()
    if btime is None:
        return None
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def _ps_start_time(pid: int) -> float | None:
    try:
        p = subprocess.run(
            ["ps", "-o", "lstart=,stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.SubprocessError:
        return None
    line = p.stdout.strip()
    if p.returncode != 0 or not line:
        return None
    # "Sat Nov 14 02:35:27 2026 Ss": первые 5 слов — время, дальше состояние.
    parts = line.split()
    if len(parts) < 5:
        return None
    if len(parts) > 5 and parts[5].startswith("Z"):
        return None  # зомби уже не процесс
    return datetime.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y").timestamp()


def _win_start_time(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime  # 100 нс с 1601-01-01
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)
