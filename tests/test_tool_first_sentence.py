"""Every tool's first sentence must survive Hermes' tiered-disclosure clip.

A deferred tool reaches the model as its name plus ``_short_desc(description, 60)``: the
first sentence, and if that is longer than 60 characters the last whole word is dropped
and an ellipsis appended. A clipped first sentence is the only thing the model has to
decide whether to load the tool at all, so "Send an encrypted message to another agent
(or a…" was the whole pitch for ``rine_send``.

🔴 The rule under test is taken from the HOST wherever the host is present: the module is
resolved out of the installed Hermes and the expected value is recomputed from that
module's own ``_SENTENCE_END_RE``. A gate that only ever asserted our own strings against
our own copy of the rule would pin whatever we shipped, clipped or not.

Three tiers, deliberately distinguished:

- **Hermes installed, clip found** (the rig): full assertion, host rule, round-trip.
- **Hermes installed, clip NOT found**: ``pytest.fail`` — the clip model moved and this
  gate has exactly one job. Never a skip; a skip here is indistinguishable from a pass.
- **Hermes not installed at all** (a bare host): the ≤60 assertion still runs, against a
  local copy of the rule, and only the round-trip leg is skipped, by name.
"""

from __future__ import annotations

import importlib
import importlib.util
import re

import pytest

from hermes_rine._schemas import SCHEMAS

MAX_CHARS = 60

# ``_short_desc`` moved modules in an upstream decomposition release (it is in
# ``tools.tool_search`` at the advertised floor and ``tools.tool_search_catalog``
# later), so both names are tried, newest first.
_CLIP_MODULE_NAMES = ("tools.tool_search_catalog", "tools.tool_search")

# Only for the no-Hermes tier, where there is no host rule to read. Copied from the
# host's own ``_SENTENCE_END_RE``: a terminator counts when followed by whitespace or
# end-of-string, and ``e.g.`` / ``i.e.`` / ``etc.`` do not end a sentence.
_LOCAL_SENTENCE_END_RE = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)[.!?](?=\s|$)")


def _hermes_installed() -> bool:
    """Whether the real Hermes is on the path (this suite stubs ``gateway``/``tools``)."""
    return importlib.util.find_spec("hermes_cli") is not None


def _clip_module():
    """The installed Hermes module that owns the clip, or None.

    ``hasattr`` on BOTH symbols is the real-vs-stub discriminator: this suite installs
    minimal ``tools.*`` stubs when Hermes is absent, so an import alone proves nothing.
    """
    for name in _CLIP_MODULE_NAMES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "_short_desc") and hasattr(module, "_SENTENCE_END_RE"):
            return module
    return None


def _first_sentence(pattern: re.Pattern[str], description: str) -> str:
    """The host's first-sentence rule, re-implemented from whichever regex is passed in."""
    text = " ".join((description or "").split())
    match = pattern.search(text)
    return text[: match.end()] if match else text


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_first_sentence_survives_the_catalog_clip(name: str) -> None:
    module = _clip_module()
    if module is None and _hermes_installed():
        pytest.fail(
            "upstream _short_desc not found in either module — the clip model moved. "
            f"Looked in {_CLIP_MODULE_NAMES}. Add the new module name here rather than "
            "weakening the assertion."
        )

    description = SCHEMAS[name]["description"]
    pattern = module._SENTENCE_END_RE if module is not None else _LOCAL_SENTENCE_END_RE
    sentence = _first_sentence(pattern, description)

    assert len(sentence) <= MAX_CHARS, (
        f"{name}: first sentence is {len(sentence)} chars, over the {MAX_CHARS} the "
        f"catalog shows: {sentence!r}"
    )

    if module is None:
        pytest.skip(
            "round-trip leg needs the real _short_desc: run this suite in the rig "
            "(compose.hermes.yml), where Hermes is installed"
        )
    # The property that actually matters: what the model is shown IS the sentence,
    # with no word dropped and no ellipsis.
    assert module._short_desc(description, MAX_CHARS) == sentence


def test_the_clip_really_does_clip() -> None:
    # Mutation guard: proves the assertion above can fail, so a host whose _short_desc
    # became the identity function could not make this gate vacuously green.
    module = _clip_module()
    if module is None:
        if _hermes_installed():
            pytest.fail("upstream _short_desc not found; see the parametrized gate above.")
        pytest.skip("no real _short_desc to mutation-check off-Hermes; run it in the rig")
    long_sentence = "Send an encrypted message to another agent or a group on the network."
    assert module._short_desc(long_sentence, MAX_CHARS).endswith("…")
    assert len(_first_sentence(module._SENTENCE_END_RE, long_sentence)) > MAX_CHARS
