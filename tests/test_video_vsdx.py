# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for Visio (.vsdx) extraction and on-prem video transcription."""

import io
import zipfile

import pytest

from threadweave.connectors.sharepoint.processor import DocumentProcessor
from threadweave.connectors.sharepoint import video


# ── .vsdx extraction ──────────────────────────────────────────────

def _vsdx_bytes(pages: list[str]) -> bytes:
    """Build a minimal .vsdx zip with visio/pages/pageN.xml files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("visio/document.xml", "<VisioDocument/>")
        for i, body in enumerate(pages, start=1):
            zf.writestr(
                f"visio/pages/page{i}.xml",
                '<?xml version="1.0"?>'
                '<PageContents xmlns="http://schemas.microsoft.com/office/'
                'visio/2012/main">'
                f"{body}"
                "</PageContents>",
            )
    return buf.getvalue()


def _shape(text: str) -> str:
    return (f'<Shape ID="1">'
            f'<Text>{text}</Text>'
            f'<Shape ID="2"><Text>{text} (nested)</Text></Shape>'
            f"</Shape>")


@pytest.fixture
def proc(tmp_path):
    return DocumentProcessor(graph_client=None, temp_dir=str(tmp_path))


def test_vsdx_shape_text(proc):
    vsdx = _vsdx_bytes([_shape("Deployment decision"), _shape("Rollback plan")])
    out = proc._extract_vsdx(vsdx)
    assert "Deployment decision" in out
    assert "Rollback plan" in out
    # nested shape text is included
    assert "Deployment decision (nested)" in out


def test_vsdx_page_markers(proc):
    out = proc._extract_vsdx(_vsdx_bytes([_shape("One"), _shape("Two")]))
    assert "[Page 1]" in out
    assert "[Page 2]" in out


def test_vsdx_bad_zip(proc):
    assert proc._extract_vsdx(b"not a zip") == ""


def test_vsdx_no_pages(proc):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("visio/document.xml", "<VisioDocument/>")
    assert proc._extract_vsdx(buf.getvalue()) == ""


def test_extensions_registered():
    assert ".vsdx" in DocumentProcessor.SUPPORTED_EXTENSIONS
    for ext in (".mp4", ".mkv", ".mov", ".webm", ".mp3", ".wav", ".m4a"):
        assert ext in DocumentProcessor.SUPPORTED_EXTENSIONS


# ── video module (fakes — no model download in tests) ─────────────

def test_transcribe_video_skips_without_whisper(monkeypatch):
    monkeypatch.setattr(video, "WHISPER_AVAILABLE", False)
    assert video.transcribe_video(b"fake video bytes") == ""


def test_transcribe_video_skips_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(video, "WHISPER_AVAILABLE", True)
    monkeypatch.setattr(video, "ffmpeg_available", lambda: False)
    assert video.transcribe_video(b"fake video bytes") == ""


def test_transcribe_video_full_pipeline(monkeypatch, tmp_path):
    """ffmpeg extracts audio, the model transcribes it, text returns."""
    calls = {}

    def fake_ffmpeg(args, capture_output=None, timeout=None):
        calls["ffmpeg_args"] = args
        # simulate ffmpeg writing the wav (it's the last arg)
        from pathlib import Path
        Path(args[-1]).write_bytes(b"WAVDATA")
        return _FakeResult(0, b"", b"")

    class _FakeSeg:
        def __init__(self, text):
            self.text = text

    class _FakeSegments:
        def __iter__(self):
            return iter([
                _FakeSeg("We decided to use Redis for the cache"),
                _FakeSeg("This fixed the latency problem"),
            ])

    class _FakeModel:
        def transcribe(self, path, beam_size=None, vad_filter=None):
            return _FakeSegments(), {}

    monkeypatch.setattr(video, "WHISPER_AVAILABLE", True)
    monkeypatch.setattr(video.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(video, "_get_model", lambda: _FakeModel())

    out = video.transcribe_video(b"fake video bytes")
    assert "We decided to use Redis" in out
    assert "latency problem" in out
    assert "-ar" in calls["ffmpeg_args"]  # 16k mono extraction


def test_transcribe_video_ffmpeg_failure(monkeypatch):
    def fake_ffmpeg(args, capture_output=None, timeout=None):
        return _FakeResult(1, b"", b"ffmpeg error: bad file")

    monkeypatch.setattr(video, "WHISPER_AVAILABLE", True)
    monkeypatch.setattr(video, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(video.subprocess, "run", fake_ffmpeg)
    assert video.transcribe_video(b"garbage") == ""


def test_transcribe_audio_writes_temp_with_ext(monkeypatch, tmp_path):
    """Audio path uses the probed extension for the temp file."""
    calls = {}

    def fake_model():
        class _Seg:
            def __init__(self, text):
                self.text = text

        class _Segs:
            def __iter__(self):
                return iter([_Seg("decision text")])

        class _M:
            def transcribe(self, path, beam_size=None, vad_filter=None):
                calls["path"] = str(path)
                return _Segs(), {}

        return _M()

    monkeypatch.setattr(video, "WHISPER_AVAILABLE", True)
    monkeypatch.setattr(video, "_get_model", fake_model)
    # mp3 magic bytes (ID3 header)
    out = video.transcribe_audio(b"ID3\x03\x00fake")
    assert "decision text" in out
    assert calls["path"].endswith(".mp3")


class _FakeResult:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
