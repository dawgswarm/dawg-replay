"""Материалы приёмки M2: сломанная копия эталона Ozon для сценария C."""

import difflib
from pathlib import Path

from dawg_replay_kit.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[3]
GOOD = ROOT / "examples" / "ozon-search-cards" / "script.py"
BROKEN = ROOT / "docs" / "acceptance" / "m2" / "ozon-search-cards-broken.py"


def test_broken_differs_only_in_version_and_api_path():
    good = GOOD.read_text(encoding="utf-8").splitlines()
    broken = BROKEN.read_text(encoding="utf-8").splitlines()
    changed = [l for l in difflib.unified_diff(good, broken, lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    assert len(changed) == 4, (
        "эталон Ozon изменился: пересоберите сломанную копию (docs/acceptance/m2.md, раздел 2.4)\n" + "\n".join(changed)
    )
    assert any("/page/json/v3" in l for l in changed if l.startswith("+"))


def test_broken_is_next_version_of_same_script():
    good, broken = load_manifest(GOOD), load_manifest(BROKEN)
    assert broken.name == good.name
    assert broken.version == good.version + 1
