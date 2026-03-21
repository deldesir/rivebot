"""
Tests for rivebot.siyuan_sync — specifically the .sy JSON parser.

The .sy format is SiYuan's internal note format. This file validates that
the parser correctly extracts rivescript code blocks from a realistic fixture.
"""

import json
import pytest
from pathlib import Path

from rivebot.siyuan_sync import _extract_rivescript_from_sy, _collect_rivescript_blocks


# ── Fixtures ──────────────────────────────────────────────────────────────────

MINIMAL_SY = {
    "Properties": {"title": "talkprep"},
    "Children": [
        {
            "Type": "NodeCodeBlock",
            "Children": [
                {"Type": "NodeCodeBlockFenceInfoMarker", "Data": "rivescript"},
                {"Type": "NodeCodeBlockCode", "Data": "> topic default\n  + help\n  - hello\n"},
            ],
        }
    ],
}

MULTI_BLOCK_SY = {
    "Properties": {"title": "Konex Support"},
    "Children": [
        {
            "Type": "NodeParagraph",
            "Data": "Intro paragraph",
            "Children": [],
        },
        {
            "Type": "NodeCodeBlock",
            "Children": [
                {"Type": "NodeCodeBlockFenceInfoMarker", "Data": "rivescript"},
                {"Type": "NodeCodeBlockCode", "Data": "! version = 2.0\n"},
            ],
        },
        {
            "Type": "NodeCodeBlock",
            "Children": [
                {"Type": "NodeCodeBlockFenceInfoMarker", "Data": "python"},  # should be ignored
                {"Type": "NodeCodeBlockCode", "Data": "print('ignored')\n"},
            ],
        },
        {
            "Type": "NodeCodeBlock",
            "Children": [
                {"Type": "NodeCodeBlockFenceInfoMarker", "Data": "rivescript"},
                {"Type": "NodeCodeBlockCode", "Data": "> topic random\n  + help\n  - ok\n"},
            ],
        },
    ],
}

NESTED_SY = {
    "Properties": {"title": "nested-test"},
    "Children": [
        {
            "Type": "NodeBlockquote",
            "Children": [
                {
                    "Type": "NodeCodeBlock",
                    "Children": [
                        {"Type": "NodeCodeBlockFenceInfoMarker", "Data": "rivescript"},
                        {"Type": "NodeCodeBlockCode", "Data": "+ nested\n- found\n"},
                    ],
                }
            ],
        }
    ],
}

NO_RIVESCRIPT_SY = {
    "Properties": {"title": "just-notes"},
    "Children": [
        {"Type": "NodeParagraph", "Data": "hello", "Children": []},
    ],
}

UNTITLED_SY = {
    "Properties": {},
    "Children": [],
}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestExtractRivescript:
    def test_minimal_block(self, tmp_path):
        f = tmp_path / "talkprep.sy"
        f.write_text(json.dumps(MINIMAL_SY))
        result = _extract_rivescript_from_sy(f)
        assert result is not None
        persona, content = result
        assert persona == "talkprep"
        assert "+ help" in content
        assert "- hello" in content

    def test_persona_name_is_lowercased_and_slugified(self, tmp_path):
        """Title 'Konex Support' → persona 'konex-support'."""
        f = tmp_path / "konex.sy"
        f.write_text(json.dumps(MULTI_BLOCK_SY))
        result = _extract_rivescript_from_sy(f)
        assert result is not None
        persona, _ = result
        assert persona == "konex-support"

    def test_only_rivescript_fences_extracted(self, tmp_path):
        """Python code blocks inside the same doc are ignored."""
        f = tmp_path / "konex.sy"
        f.write_text(json.dumps(MULTI_BLOCK_SY))
        _, content = _extract_rivescript_from_sy(f)
        assert "! version = 2.0" in content
        assert "> topic random" in content
        assert "print('ignored')" not in content

    def test_nested_blocks_are_found(self, tmp_path):
        """Rivescript blocks nested inside blockquotes are extracted."""
        f = tmp_path / "nested.sy"
        f.write_text(json.dumps(NESTED_SY))
        result = _extract_rivescript_from_sy(f)
        assert result is not None
        _, content = result
        assert "+ nested" in content

    def test_no_rivescript_returns_none(self, tmp_path):
        """Docs with no rivescript fences return None."""
        f = tmp_path / "notes.sy"
        f.write_text(json.dumps(NO_RIVESCRIPT_SY))
        assert _extract_rivescript_from_sy(f) is None

    def test_no_title_returns_none(self, tmp_path):
        """Docs without a title return None."""
        f = tmp_path / "untitled.sy"
        f.write_text(json.dumps(UNTITLED_SY))
        assert _extract_rivescript_from_sy(f) is None

    def test_invalid_json_returns_none(self, tmp_path):
        """Corrupt .sy files don't crash — return None."""
        f = tmp_path / "bad.sy"
        f.write_text("not json {{{{")
        assert _extract_rivescript_from_sy(f) is None

    def test_multiple_blocks_are_joined(self, tmp_path):
        """Multiple rivescript fences in one doc are joined with blank lines."""
        f = tmp_path / "multi.sy"
        f.write_text(json.dumps(MULTI_BLOCK_SY))
        _, content = _extract_rivescript_from_sy(f)
        # Both blocks present
        assert "! version = 2.0" in content
        assert "> topic random" in content
        # Joined by double newline
        assert "\n\n" in content
