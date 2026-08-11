"""Artifact summarisation: never dump full artifact bodies into a tool
result (anti-goal, build brief §6). Pure functions -- no I/O.

Known gap, recorded rather than hidden: there is no "fetch full artifact"
follow-up call wired up yet. Binary/url/structured parts are described
(mime type, byte length) but never inlined; text is truncated past
MAX_INLINE_TEXT_CHARS with the original length reported alongside. A real
detail-fetch mechanism (most naturally an MCP `resources/read` URI per
artifact) is future work -- see README.md.
"""

from __future__ import annotations

from typing import Any

MAX_INLINE_TEXT_CHARS = 4000


def _summarize_part(part: Any) -> dict | None:
    kind = part.WhichOneof("content")
    if kind == "text":
        return None  # text parts are concatenated into the artifact's main text, not listed separately
    if kind == "raw":
        summary = {"kind": "binary", "mediaType": part.media_type or "application/octet-stream", "byteLength": len(part.raw)}
        if part.filename:
            summary["filename"] = part.filename
        return summary
    if kind == "url":
        summary = {"kind": "url", "url": part.url}
        if part.media_type:
            summary["mediaType"] = part.media_type
        return summary
    if kind == "data":
        return {"kind": "structured", "note": "structured data part present; not inlined"}
    return {"kind": "unknown"}


def summarize_artifact(artifact: Any) -> dict:
    """One A2A Artifact -> one MCP-content-shaped summary dict."""
    text = "\n".join(part.text for part in artifact.parts if part.WhichOneof("content") == "text")
    original_length = len(text)
    truncated = original_length > MAX_INLINE_TEXT_CHARS
    if truncated:
        text = text[:MAX_INLINE_TEXT_CHARS]

    other_parts = [p for p in (_summarize_part(part) for part in artifact.parts) if p is not None]

    summary: dict[str, Any] = {
        "artifactId": artifact.artifact_id,
        "content": {"type": "text", "text": text},
    }
    if artifact.name:
        summary["name"] = artifact.name
    if truncated:
        summary["truncated"] = True
        summary["originalLength"] = original_length
    if other_parts:
        summary["parts"] = other_parts
    return summary


def summarize_artifacts(artifacts: list) -> list[dict]:
    return [summarize_artifact(a) for a in artifacts]
