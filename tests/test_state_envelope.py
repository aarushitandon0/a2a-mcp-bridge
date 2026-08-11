from __future__ import annotations

import pytest

from bridge import state

KEY_A = b"\x01" * 32
KEY_B = b"\x02" * 32


def _handle(**overrides):
    defaults = dict(
        agent_id="docs",
        a2a_task_id="5f2f1c1a-1111-4a2b-9c3d-0123456789ab",
        a2a_context_id="5f2f1c1a-2222-4a2b-9c3d-0123456789ab",
        exp=1_000_000 + 3600,
    )
    defaults.update(overrides)
    return state.TaskHandle(**defaults)


def test_seal_unseal_round_trip():
    handle = _handle()
    token = state.seal(handle, key=KEY_A)
    result = state.unseal(token, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_100)
    assert result == handle


def test_seal_unseal_round_trip_with_non_uuid_ids():
    # A2A doesn't mandate UUID-formatted task/context ids -- must not corrupt
    # or crash on an agent that uses opaque non-UUID strings.
    handle = _handle(a2a_task_id="task-abc-123", a2a_context_id="ctx-xyz")
    token = state.seal(handle, key=KEY_A)
    result = state.unseal(token, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_100)
    assert result == handle


def test_handle_size_is_compact_for_a_short_agent_slug():
    # Two full 128-bit UUIDs + AES-GCM's 28-byte nonce/tag overhead is ~60
    # unavoidable raw bytes before any other field -- under 100 base64 chars
    # is only reachable with a short agent_id slug (SPEC-NOTES.md §12a), not
    # guaranteed for arbitrarily long agent ids. "docs" (4 chars) is the
    # realistic case this bridge is tuned for.
    token = state.seal(_handle(agent_id="docs"), key=KEY_A)
    assert len(token) < 100, f"envelope is {len(token)} chars, expected under 100"


def test_handle_size_grows_with_agent_id_length():
    short = state.seal(_handle(agent_id="d"), key=KEY_A)
    long = state.seal(_handle(agent_id="docs-agent-production"), key=KEY_A)
    assert len(long) > len(short)


# -- Four rejection paths + bit-flip tamper test, per SPEC-NOTES.md §12.


def test_rejects_bad_mac_wrong_key():
    token = state.seal(_handle(), key=KEY_A)
    with pytest.raises(state.TamperedEnvelope):
        state.unseal(token, keys=[KEY_B], known_agent_ids={"docs"}, now=1_000_100)


def test_rejects_bit_flipped_ciphertext():
    token = state.seal(_handle(), key=KEY_A)
    raw = bytearray(state._b64url_decode(token))
    raw[-1] ^= 0x01  # flip a bit inside the ciphertext/tag
    tampered = state._b64url_encode(bytes(raw))
    with pytest.raises(state.TamperedEnvelope):
        state.unseal(tampered, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_100)


def test_rejects_expired_envelope():
    handle = _handle(exp=1_000_100)
    token = state.seal(handle, key=KEY_A)
    with pytest.raises(state.ExpiredEnvelope) as exc_info:
        state.unseal(token, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_100)
    assert exc_info.value.handle == handle


def test_expiry_is_exclusive_at_the_boundary():
    handle = _handle(exp=1_000_100)
    token = state.seal(handle, key=KEY_A)
    # now == exp is already expired (SPEC-NOTES.md: "exp has passed")
    with pytest.raises(state.ExpiredEnvelope):
        state.unseal(token, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_100)
    # one second before exp is still valid
    state.unseal(token, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_099)


def test_rejects_unknown_agent_not_in_allowlist():
    token = state.seal(_handle(agent_id="docs"), key=KEY_A)
    with pytest.raises(state.UnknownAgent):
        state.unseal(token, keys=[KEY_A], known_agent_ids={"other-agent"}, now=1_000_100)


def test_rejects_unknown_envelope_version(monkeypatch):
    monkeypatch.setattr(state, "ENVELOPE_VERSION", 999)
    token = state.seal(_handle(), key=KEY_A)
    monkeypatch.setattr(state, "ENVELOPE_VERSION", 2)
    with pytest.raises(state.UnknownEnvelopeVersion):
        state.unseal(token, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_100)


def test_rejects_garbage_token():
    with pytest.raises(state.TamperedEnvelope):
        state.unseal("not-a-real-token", keys=[KEY_A], known_agent_ids={"docs"}, now=0)


def test_rejects_truncated_token():
    with pytest.raises(state.TamperedEnvelope):
        state.unseal(
            state._b64url_encode(b"short"), keys=[KEY_A], known_agent_ids={"docs"}, now=0
        )


# -- Key rotation (SPEC-NOTES.md §12b): unseal must accept current + previous.


def test_key_rotation_unseal_accepts_previous_key():
    handle = _handle()
    token = state.seal(handle, key=KEY_A)  # sealed under the "old" key
    # after rotation, KEY_B is current, KEY_A is previous
    result = state.unseal(token, keys=[KEY_B, KEY_A], known_agent_ids={"docs"}, now=1_000_100)
    assert result == handle


def test_seal_always_uses_the_first_supplied_key():
    handle = _handle()
    token = state.seal(handle, key=KEY_B)
    with pytest.raises(state.TamperedEnvelope):
        state.unseal(token, keys=[KEY_A], known_agent_ids={"docs"}, now=1_000_100)
    state.unseal(token, keys=[KEY_B], known_agent_ids={"docs"}, now=1_000_100)


# -- Key loading from env


def test_load_keys_from_env_current_only():
    import base64

    env = {"BRIDGE_STATE_KEY": base64.urlsafe_b64encode(KEY_A).decode()}
    keys = state.load_keys_from_env(env)
    assert keys == [KEY_A]


def test_load_keys_from_env_current_and_previous():
    import base64

    env = {
        "BRIDGE_STATE_KEY": base64.urlsafe_b64encode(KEY_B).decode(),
        "BRIDGE_STATE_KEY_PREVIOUS": base64.urlsafe_b64encode(KEY_A).decode(),
    }
    keys = state.load_keys_from_env(env)
    assert keys == [KEY_B, KEY_A]


def test_load_keys_from_env_missing_current_raises():
    with pytest.raises(state.ConfigError):
        state.load_keys_from_env({})


def test_load_key_wrong_length_raises():
    import base64

    short_key = base64.urlsafe_b64encode(b"too-short").decode()
    with pytest.raises(state.ConfigError):
        state.load_key(short_key, source="BRIDGE_STATE_KEY")
