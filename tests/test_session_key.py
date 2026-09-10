"""The session key the post-delivery callback is registered under.

Everything exactly-once rests on the gateway handing our callback back, and it looks the
callback up by session key. ``handle_message`` builds that key with the adapter's own
``_session_key_profile``; ``_session_key_for`` has to build the same one. Omitting the
``profile`` keyword was invisible on a single-profile gateway and, on a multiplexing one,
keyed the callback under ``agent:main`` while the turn ran under ``agent:<profile>`` — so
the callback was never popped, the message was never journaled or acked, and it
re-dispatched every poll forever.
"""

from __future__ import annotations

import pytest

from hermes_rine._poll import _dispatch, _session_key_for


def _source(adapter) -> object:
    """A DM source, built exactly as ``_dispatch`` builds one."""
    return adapter.build_source(
        chat_id="alice",
        chat_name="alice",
        chat_type="dm",
        user_id="alice",
        user_name="alice",
        message_id="m1",
    )


def test_a_single_profile_gateway_keys_byte_identically_to_before(make_adapter) -> None:
    # 🔴 The regression guard for the fix itself: passing profile=None must produce the
    # very same string a caller that passes no profile at all produces, or closing the
    # multi-profile hole would have moved every existing single-profile key.
    from gateway.platforms.base import build_session_key  # type: ignore[import-not-found]

    adapter = make_adapter()
    source = _source(adapter)

    assert adapter._session_key_profile(source) is None
    assert _session_key_for(adapter, source) == build_session_key(
        source, group_sessions_per_user=True, thread_sessions_per_user=False
    )


def test_a_named_profile_keys_under_its_own_namespace(make_adapter) -> None:
    adapter = make_adapter()
    adapter._owner_profile = "work"

    assert _session_key_for(adapter, _source(adapter)).startswith("agent:work:")


def test_a_host_below_the_floor_degrades_loudly_instead_of_raising(
    make_adapter, monkeypatch, caplog
) -> None:
    """🔴 Users below the advertised floor are unsupported, not broken.

    ``build_session_key`` grew ``profile`` at v2026.8.3 and
    ``BasePlatformAdapter._session_key_profile`` only at v2026.8.19, so the two halves do
    not arrive together and a guard on either alone leaves a real band uncovered. Below
    either boundary the unguarded call raises inside ``_dispatch``, ``_poll_loop``'s
    catch-all swallows it as a generic poll error, and the agent stops waking on mail
    entirely — a total loss of function, not a degraded one.
    """
    import gateway.platforms.base as base  # type: ignore[import-not-found]

    class OldBase:
        """v0.16.0's adapter base: no profile resolver anywhere on it."""

    def old_build_session_key(
        source, group_sessions_per_user=True, thread_sessions_per_user=False
    ) -> str:
        return f"agent:main:rine:dm:{getattr(source, 'chat_id', 'alice')}"

    monkeypatch.setattr(base, "BasePlatformAdapter", OldBase)
    monkeypatch.setattr(base, "build_session_key", old_build_session_key)
    adapter = make_adapter()
    adapter._owner_profile = "work"

    with caplog.at_level("WARNING", logger="hermes_rine.poll"):
        key = _session_key_for(adapter, _source(adapter))

    assert key.startswith("agent:main:")
    assert "v2026.8.19" in caplog.text and "v2026.8.31" in caplog.text


def test_a_supported_host_still_demands_the_adapters_own_resolver(
    make_adapter, monkeypatch
) -> None:
    # 🔴 The guard probes the HOST's BasePlatformAdapter, never ``type(adapter)``, and
    # this is why. A getattr on the adapter would turn every adapter that forgot the
    # method — a test double above all — into a silent agent:main key, which is exactly
    # the multi-profile defect the profile keyword was added to close. On a host that has
    # the mechanism, a missing resolver is still an AttributeError.
    monkeypatch.delattr(type(make_adapter()), "_session_key_profile")
    adapter = make_adapter()

    with pytest.raises(AttributeError):
        _session_key_for(adapter, _source(adapter))


async def test_the_callback_is_registered_under_the_profiles_key(
    make_adapter, make_message
) -> None:
    # The assertion that matters in production: the key the callback lands under is the
    # profile's, not agent:main's. Under agent:main the gateway pops nothing.
    adapter = make_adapter()
    adapter._owner_profile = "work"

    await _dispatch(adapter, make_message(sender_handle="alice"))

    [session_key] = list(adapter._post_delivery_callbacks)
    assert session_key.startswith("agent:work:")
