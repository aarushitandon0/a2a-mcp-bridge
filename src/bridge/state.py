"""The sealed task-handle envelope -- this bridge's only "state."

Design and provenance: SPEC-NOTES.md §12 (supersedes the original build
brief's §4 requestState design -- see that section for why). The envelope
IS the MCP `taskId` handed back from tools/call; any bridge instance can
unseal it to recover which A2A agent/task/context it refers to, so no
server-side task store is needed anywhere (proven in M4).

Plaintext payload: a CBOR **array** (positional, not a keyed map -- skips
per-field key bytes entirely, which matters at this size budget):
  [0] v            envelope format version (int)
  [1] agent_id     short id, resolved against config.py's static agent table
  [2] a2a_task_id      16 raw bytes if the id parses as a UUID, else the string
  [3] a2a_context_id   same encoding rule
  [4] exp          expiry, unix seconds -- tracks the A2A task's own ttlMs,
                    not an arbitrary short window (SPEC-NOTES.md §12c)

No `iat`: dropped for size (SPEC-NOTES.md §12a) -- it wasn't
security/correctness-load-bearing, only cosmetic (a stable `createdAt` for
the MCP Task response). tasks.py reports `createdAt == lastUpdatedAt` from
A2A's own status timestamp instead; see that module for the trade-off this
accepts.

Sealing: AES-256-GCM (`cryptography` hazmat AEAD), random 12-byte nonce per
seal. Wire format: base64url(nonce || ciphertext_with_tag), no padding.

Honest size result (SPEC-NOTES.md §12a): two full 128-bit UUIDs plus
AES-GCM's 12-byte nonce + 16-byte tag is 60 bytes of unavoidable overhead
before a single other field is added -- under-100-char handles are only
achieved with a short `agent_id` slug (roughly 4 characters or less);
longer agent ids push past it regardless of encoding. Operators should
configure short `agent_id` values for this reason.

Unseal is fed untrusted client input (the client echoes the taskId back
verbatim, per MCP's opacity contract -- but nothing stops a client from
tampering with it). Every rejection reason below is a distinct exception
so callers can produce a clear, specific error rather than a generic
"not found" -- required per SPEC-NOTES.md §12c: a client presenting an
expired handle for a task that may still be running agent-side needs to
know that's what happened, not get a bare 404-shaped answer.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import cbor2
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_VERSION = 2
_NONCE_LEN = 12
_KEY_LEN = 32  # AES-256

# Positional indices into the CBOR array payload -- see module docstring.
_IDX_V, _IDX_AGENT_ID, _IDX_A2A_TASK_ID, _IDX_A2A_CONTEXT_ID, _IDX_EXP = range(5)


class EnvelopeError(Exception):
    """Base class for every reason a taskId handle can be rejected."""


class TamperedEnvelope(EnvelopeError):
    """AEAD authentication failed -- wrong key, or the ciphertext was altered."""


class ExpiredEnvelope(EnvelopeError):
    """The envelope's `exp` has passed. The task itself is now unaddressable
    through this bridge, even if it's still running agent-side -- see
    SPEC-NOTES.md §12c."""

    def __init__(self, handle: "TaskHandle"):
        super().__init__(f"task handle expired at {handle.exp} (agent_id={handle.agent_id!r})")
        self.handle = handle


class UnknownEnvelopeVersion(EnvelopeError):
    pass


class UnknownAgent(EnvelopeError):
    """The envelope decoded and verified, but names an agent_id this bridge
    instance isn't configured with -- e.g. removed from config, or a token
    forged/reused against the wrong deployment."""


class ConfigError(Exception):
    """Malformed BRIDGE_STATE_KEY / BRIDGE_STATE_KEY_PREVIOUS."""


@dataclass(frozen=True)
class TaskHandle:
    agent_id: str
    a2a_task_id: str
    a2a_context_id: str
    exp: int


def _b64url_encode(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    import base64

    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded)


def load_key(env_value: str, *, source: str) -> bytes:
    """Decode a base64url-encoded 32-byte AES-256 key from an env var value."""
    try:
        key = _b64url_decode(env_value)
    except Exception as exc:
        raise ConfigError(f"{source} is not valid base64url: {exc}") from exc
    if len(key) != _KEY_LEN:
        raise ConfigError(f"{source} must decode to {_KEY_LEN} bytes, got {len(key)}")
    return key


def load_keys_from_env(env: dict[str, str] | None = None) -> list[bytes]:
    """Current key first, then the previous key if set (SPEC-NOTES.md §12b)."""
    env = env if env is not None else os.environ
    current = env.get("BRIDGE_STATE_KEY")
    if not current:
        raise ConfigError("BRIDGE_STATE_KEY environment variable is not set")
    keys = [load_key(current, source="BRIDGE_STATE_KEY")]
    previous = env.get("BRIDGE_STATE_KEY_PREVIOUS")
    if previous:
        keys.append(load_key(previous, source="BRIDGE_STATE_KEY_PREVIOUS"))
    return keys


def _pack_id(value: str) -> bytes | str:
    try:
        return uuid.UUID(value).bytes
    except (ValueError, AttributeError):
        return value


def _unpack_id(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return str(uuid.UUID(bytes=value))
    return value


def seal(handle: TaskHandle, *, key: bytes) -> str:
    payload = cbor2.dumps(
        [
            ENVELOPE_VERSION,
            handle.agent_id,
            _pack_id(handle.a2a_task_id),
            _pack_id(handle.a2a_context_id),
            handle.exp,
        ]
    )
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, payload, None)
    return _b64url_encode(nonce + ciphertext)


def unseal(token: str, *, keys: list[bytes], known_agent_ids: set[str], now: int) -> TaskHandle:
    try:
        raw = _b64url_decode(token)
    except Exception as exc:
        raise TamperedEnvelope(f"taskId is not valid base64url: {exc}") from exc

    if len(raw) <= _NONCE_LEN:
        raise TamperedEnvelope("taskId is too short to contain a nonce and ciphertext")
    nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]

    payload = None
    for key in keys:
        try:
            payload = AESGCM(key).decrypt(nonce, ciphertext, None)
            break
        except InvalidTag:
            continue
    if payload is None:
        raise TamperedEnvelope("taskId failed authentication against all configured keys")

    try:
        fields = cbor2.loads(payload)
    except Exception as exc:
        raise TamperedEnvelope(f"taskId decrypted but is not valid CBOR: {exc}") from exc

    if not isinstance(fields, list) or len(fields) != 5:
        raise TamperedEnvelope("taskId decrypted but is not a 5-element CBOR array")

    version = fields[_IDX_V]
    if version != ENVELOPE_VERSION:
        raise UnknownEnvelopeVersion(f"unknown envelope version: {version!r}")

    handle = TaskHandle(
        agent_id=fields[_IDX_AGENT_ID],
        a2a_task_id=_unpack_id(fields[_IDX_A2A_TASK_ID]),
        a2a_context_id=_unpack_id(fields[_IDX_A2A_CONTEXT_ID]),
        exp=fields[_IDX_EXP],
    )

    if handle.agent_id not in known_agent_ids:
        raise UnknownAgent(f"unknown agent_id in task handle: {handle.agent_id!r}")
    if now >= handle.exp:
        raise ExpiredEnvelope(handle)

    return handle
