from importlib.metadata import version

import dawg_replay_kit
from dawg_replay_kit.manifest import SUPPORTED_CONTRACTS


def test_version_single_source():
    # pyproject берёт версию из __version__ (hatch dynamic), установленный пакет должен совпадать.
    assert version("dawg-replay-kit") == dawg_replay_kit.__version__


def test_kit_major_matches_contract():
    assert int(dawg_replay_kit.__version__.split(".")[0]) == max(SUPPORTED_CONTRACTS)
