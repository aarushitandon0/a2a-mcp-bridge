from __future__ import annotations

from a2a.types import Artifact, Part

from bridge import artifacts


def test_summarize_text_artifact():
    artifact = Artifact(artifact_id="a1", name="Report", parts=[Part(text="Hello world")])
    summary = artifacts.summarize_artifact(artifact)
    assert summary["artifactId"] == "a1"
    assert summary["name"] == "Report"
    assert summary["content"] == {"type": "text", "text": "Hello world"}
    assert "truncated" not in summary


def test_summarize_truncates_long_text_and_reports_original_length():
    long_text = "x" * (artifacts.MAX_INLINE_TEXT_CHARS + 500)
    artifact = Artifact(artifact_id="a1", parts=[Part(text=long_text)])
    summary = artifacts.summarize_artifact(artifact)
    assert len(summary["content"]["text"]) == artifacts.MAX_INLINE_TEXT_CHARS
    assert summary["truncated"] is True
    assert summary["originalLength"] == len(long_text)


def test_summarize_never_inlines_binary_data():
    artifact = Artifact(
        artifact_id="a1",
        parts=[Part(raw=b"\x00\x01binary-content-here", media_type="application/octet-stream", filename="data.bin")],
    )
    summary = artifacts.summarize_artifact(artifact)
    assert summary["content"] == {"type": "text", "text": ""}
    part_summary = summary["parts"][0]
    assert part_summary["kind"] == "binary"
    assert part_summary["byteLength"] == len(b"\x00\x01binary-content-here")
    assert part_summary["filename"] == "data.bin"
    assert "raw" not in str(part_summary)  # never carries the actual bytes


def test_summarize_url_part():
    artifact = Artifact(artifact_id="a1", parts=[Part(url="https://example.com/report.pdf", media_type="application/pdf")])
    summary = artifacts.summarize_artifact(artifact)
    assert summary["parts"][0] == {"kind": "url", "url": "https://example.com/report.pdf", "mediaType": "application/pdf"}


def test_summarize_artifacts_list():
    a1 = Artifact(artifact_id="a1", parts=[Part(text="one")])
    a2 = Artifact(artifact_id="a2", parts=[Part(text="two")])
    summaries = artifacts.summarize_artifacts([a1, a2])
    assert [s["artifactId"] for s in summaries] == ["a1", "a2"]
