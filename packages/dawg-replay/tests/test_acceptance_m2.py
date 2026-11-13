"""Материалы приёмки M2: сломанная копия эталона Ozon для сценария C."""

import importlib.util
from pathlib import Path

from dawg_replay_kit.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[3]
GOOD = ROOT / "examples" / "ozon-search-cards" / "script.py"
BROKEN = ROOT / "docs" / "acceptance" / "m2" / "ozon-search-cards-broken.py"


def _generator():
    spec = importlib.util.spec_from_file_location("make_broken", ROOT / "docs" / "acceptance" / "m2" / "make_broken.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_broken_copy_is_up_to_date():
    expected = _generator().build(GOOD.read_text(encoding="utf-8"))
    assert BROKEN.read_text(encoding="utf-8") == expected, (
        "эталон Ozon изменился: python docs/acceptance/m2/make_broken.py"
    )


def test_broken_is_next_version_of_same_script():
    good, broken = load_manifest(GOOD), load_manifest(BROKEN)
    assert broken.name == good.name
    assert broken.version == good.version + 1
    assert "/page/json/v3" in BROKEN.read_text(encoding="utf-8")
