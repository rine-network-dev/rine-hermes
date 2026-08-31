"""Validation tests for the bundled ``rine:rine`` SKILL.md.

Hermes' skill validator requires the file to start with ``---`` at byte 0, close the
frontmatter with ``\\n---\\n``, carry a ``name`` (``[a-zA-Z0-9_-]+`` ≤64) and a
``description`` (≤1024), have a non-empty body, and weigh ≤100,000 chars. These tests
pin the shipped skill against that contract so a bad edit fails CI, not the gateway.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SKILL = (
    Path(__file__).resolve().parent.parent / "src" / "hermes_rine" / "skill" / "rine" / "SKILL.md"
)
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _frontmatter_and_body(text: str) -> tuple[dict[str, str], str]:
    """Split a frontmatter file into a ``{key: value}`` map and the body text."""
    assert text.startswith("---"), "SKILL.md must open with '---' at byte 0"
    end = text.index("\n---\n", 3)
    block = text[text.index("\n", 3) + 1 : end]
    body = text[end + len("\n---\n") :]
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, body


@pytest.fixture(scope="module")
def skill_text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def test_skill_file_exists():
    assert _SKILL.is_file()


def test_skill_starts_with_frontmatter_delimiter_at_byte_zero(skill_text):
    assert skill_text.startswith("---")


def test_skill_frontmatter_closes_with_dashes_block(skill_text):
    # Validator requires a closing '\n---\n' after the opening delimiter.
    assert "\n---\n" in skill_text[3:]


def test_skill_name_is_within_bounds(skill_text):
    meta, _ = _frontmatter_and_body(skill_text)
    name = meta.get("name", "")
    assert name == "rine"
    assert _NAME_RE.match(name)
    assert len(name) <= 64


def test_skill_description_is_present_and_within_bounds(skill_text):
    meta, _ = _frontmatter_and_body(skill_text)
    description = meta.get("description", "")
    assert description
    assert len(description) <= 1024


def test_skill_body_is_non_empty(skill_text):
    _, body = _frontmatter_and_body(skill_text)
    assert body.strip()


def test_skill_file_is_within_size_ceiling(skill_text):
    assert len(skill_text) <= 100_000


def test_skill_body_never_leaks_ciphertext_guidance(skill_text):
    """The skill teaches plaintext-only — it must not instruct reading ciphertext."""
    _, body = _frontmatter_and_body(skill_text)
    assert "encrypted_payload" not in body
