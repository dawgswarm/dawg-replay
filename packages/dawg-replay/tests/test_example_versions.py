"""Изменённый пример должен поднять `version`: иначе `dawg-replay add` у пользователя, который уже
добавлял пример, отвечает «версия N уже зарегистрирована с другим содержимым».

Отпечатки примеров лежат в example_versions.json. Поменяли пример: поднимите `version` в манифесте
и обновите отпечаток (текст ошибки теста подсказывает новые значения)."""

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RECORDS = Path(__file__).with_name("example_versions.json")


def fingerprint(path: Path) -> tuple[int, str]:
    text = path.read_bytes().replace(b"\r\n", b"\n")
    m = re.search(rb"^# version = (\d+)\s*$", text, re.M)
    assert m, f"{path}: нет `version` в манифесте"
    return int(m.group(1)), hashlib.sha256(text).hexdigest()


def problem(path: Path, record: dict | None) -> str | None:
    version, digest = fingerprint(path)
    fix = f'"version": {version}, "sha256": "{digest}"'
    if record is None:
        return f"{path.name}: нового примера нет в {RECORDS.name}, добавьте {{{fix}}}"
    if digest == record["sha256"]:
        return None if version == record["version"] else f"{path}: version не совпадает с записью"
    if version <= record["version"]:
        return (f"{path}: пример изменён, а version осталась {version}. Поднимите version до {record['version'] + 1} "
                f"и обновите запись в {RECORDS.name}")
    return f"{path}: version поднята, обновите запись в {RECORDS.name}: {{{fix}}}"


def examples():
    return sorted((ROOT / "examples").glob("*/script.py"))


@pytest.mark.parametrize("path", examples(), ids=lambda p: p.parent.name)
def test_changed_example_bumps_version(path):
    records = json.loads(RECORDS.read_text(encoding="utf-8"))
    assert problem(path, records.get(path.relative_to(ROOT).as_posix())) is None, problem(path, records.get(path.relative_to(ROOT).as_posix()))


def test_check_catches_change_without_bump(tmp_path):
    src = ROOT / "examples" / "the-internet-actions" / "script.py"
    record = dict(zip(("version", "sha256"), fingerprint(src)))
    copy = tmp_path / "script.py"
    copy.write_bytes(src.read_bytes().replace(b"\r\n", b"\n") + "\n# правка\n".encode())
    assert "version осталась" in problem(copy, record)
    copy.write_text(copy.read_text(encoding="utf-8").replace(f"# version = {record['version']}", f"# version = {record['version'] + 1}"), encoding="utf-8")
    assert "обновите запись" in problem(copy, record)
    lf = src.read_bytes().replace(b"\r\n", b"\n")  # на Windows git может отдать файл с CRLF
    copy.write_bytes(lf.replace(b"\n", b"\r\n"))  # перевод строки Windows не считается правкой
    assert problem(copy, record) is None
