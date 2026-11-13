"""Скилл dawg-replay-builder (skill/): копия контракта, формат Agent Skills, упаковка плагина."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "skill"
SKILL_DIR = PLUGIN / "skills" / "dawg-replay-builder"
SKILL = SKILL_DIR / "SKILL.md"

# Файлы, которые делает отдельный тикет; ссылки на них допустимы, пока их нет.
PENDING = {"template.py", "exploration.md"}
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def frontmatter(text: str) -> dict[str, str]:
    assert text.startswith("---\n"), "SKILL.md должен начинаться с frontmatter"
    block = text.split("\n---\n", 1)[0][4:]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_contract_copy_matches_docs():
    assert (SKILL_DIR / "contract.md").read_bytes() == (ROOT / "docs" / "contract.md").read_bytes(), (
        "skill/skills/dawg-replay-builder/contract.md отстал: cp docs/contract.md skill/skills/dawg-replay-builder/contract.md"
    )


def test_frontmatter_follows_agent_skills_spec():
    fm = frontmatter(SKILL.read_text(encoding="utf-8"))
    assert set(fm) <= SPEC_FIELDS, f"лишние поля: {set(fm) - SPEC_FIELDS}"
    name = fm["name"]
    assert name == SKILL_DIR.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
    assert fm["description"].startswith("Use when") and 1 <= len(fm["description"]) <= 1024
    assert len(fm.get("compatibility", "")) <= 500


def test_skill_size():
    text = SKILL.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 500


def test_links_point_to_files():
    text = SKILL.read_text(encoding="utf-8")
    targets = set(re.findall(r"\]\(([^)#:]+)\)", text))
    assert {"contract.md", "checklist.md", "template.py", "exploration.md"} <= targets
    for t in targets:
        if t in PENDING and not (SKILL_DIR / t).exists():
            continue
        assert (SKILL_DIR / t).is_file(), f"ссылка на несуществующий файл: {t}"


def skill_text_files():
    return [p for p in PLUGIN.rglob("*") if p.is_file() and p.suffix in (".md", ".json", ".py", ".toml")]


@pytest.mark.parametrize("path", skill_text_files(), ids=lambda p: str(p.relative_to(PLUGIN)))
def test_no_long_dashes(path):
    # Правило спеки: в коде и документации нет длинных тире.
    assert "—" not in path.read_text(encoding="utf-8")


def test_plugin_manifests_consistent():
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    market = json.loads((PLUGIN / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    mcp = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert plugin["name"] == SKILL_DIR.name
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"] == "./"
    assert market["owner"]["name"]
    server = mcp["mcpServers"]["dawg"]
    assert server["command"] == "uvx" and server["args"] == ["dawg-mcp"]
    keys = re.findall(r"\$\{user_config\.(\w+)\}", json.dumps(server))
    assert keys and all(plugin["userConfig"][k]["sensitive"] for k in keys)
