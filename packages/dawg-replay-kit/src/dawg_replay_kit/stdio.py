"""stdout и stderr скрипта в UTF-8 на любой ОС.

На Windows вывод в файл или канал идёт в кодировке локали (cp1252, cp1251), и
`print("готово")` там падает с UnicodeEncodeError или пишет то, что приложение
не прочитает: stdout.log и stderr.log оно читает как UTF-8. Kit переключает оба
потока при импорте, до первого вывода скрипта.
"""

from __future__ import annotations

import codecs
import io
import sys


def _is_utf8(stream: io.TextIOWrapper) -> bool:
    try:
        return codecs.lookup(stream.encoding).name == "utf-8"
    except (LookupError, TypeError):
        return False


def use_utf8() -> None:
    """Переводит sys.stdout и sys.stderr в UTF-8, если они в другой кодировке.

    Непредставимые символы (одиночные суррогаты) экранируются, вывод не падает.
    Потоки, подменённые скриптом или тестами (не TextIOWrapper), не трогаются.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if isinstance(stream, io.TextIOWrapper) and not _is_utf8(stream):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, io.UnsupportedOperation):
                pass  # поток уже закрыт или не поддерживает смену кодировки
