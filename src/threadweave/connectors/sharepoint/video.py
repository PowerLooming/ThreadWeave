# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""On-prem video transcription for the SharePoint connector.

The privacy contract is absolute: content flows one way M365 -> on-prem
and never out again. Transcription therefore runs entirely on the host
with faster-whisper (CTranslate2 backend) — no cloud speech services,
no audio ever leaves the machine.

Pipeline per video:
1. ffmpeg extracts the audio track to a temp WAV (16 kHz mono, the
   format Whisper expects)
2. faster-whisper transcribes it with a cached model (tiny/base/small/
   medium — configurable, default "base")
3. the transcript text flows into the normal detection/ingest path

ffmpeg must be on PATH (checked at import); faster-whisper is an
optional dependency (WHISPER_AVAILABLE flag) so the connector works
without it — videos are simply skipped, like other unsupported types.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from faster_whisper import WhisperModel  # type: ignore

    WHISPER_AVAILABLE = True
except ImportError:  # pragma: no cover
    WhisperModel = None
    WHISPER_AVAILABLE = False

FFMPEG_BIN = os.environ.get("THREADWEAVE_FFMPEG", "ffmpeg")

# Model size selection. "base" is the default: decent quality on CPU
# without a multi-GB download. Override with THREADWEAVE_WHISPER_MODEL.
DEFAULT_MODEL = os.environ.get("THREADWEAVE_WHISPER_MODEL", "base")

# Cache the model process-wide: loading takes seconds, transcription
# takes a fraction of it. The model is small enough to hold in RAM.
_model: WhisperModel | None = None
_model_name: str | None = None


def _get_model():
    """Load (once) and return the Whisper model."""
    global _model, _model_name
    if not WHISPER_AVAILABLE:
        return None
    if _model is None or _model_name != DEFAULT_MODEL:
        logger.info("Loading Whisper model '%s' (first use downloads it)",
                    DEFAULT_MODEL)
        _model = WhisperModel(DEFAULT_MODEL, device="cpu",
                              compute_type="int8")
        _model_name = DEFAULT_MODEL
    return _model


def ffmpeg_available() -> bool:
    """True when the ffmpeg binary is reachable."""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-version"], capture_output=True, timeout=10
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def transcribe_video(content: bytes, timeout: int = 600) -> str:
    """Extract audio and transcribe a video file. Returns text.

    Returns "" when transcription isn't possible (no ffmpeg, no
    whisper, empty audio, or failure) — the caller skips silently,
    matching how other unsupported formats behave.
    """
    if not WHISPER_AVAILABLE:
        logger.debug("faster-whisper not installed — skipping video")
        return ""
    if not ffmpeg_available():
        logger.debug("ffmpeg not found — skipping video")
        return ""

    with tempfile.TemporaryDirectory() as tmp:
        video_path = Path(tmp) / "input.mp4"
        audio_path = Path(tmp) / "audio.wav"
        video_path.write_bytes(content)

        # Audio extraction: 16 kHz mono WAV (Whisper's native input).
        # Two-pass is unnecessary for our sizes; a single ffmpeg call
        # with error tolerance keeps it simple.
        result = subprocess.run(
            [FFMPEG_BIN, "-y", "-i", str(video_path),
             "-vn", "-ac", "1", "-ar", "16000",
             str(audio_path)],
            capture_output=True, timeout=timeout,
        )
        if result.returncode != 0 or not audio_path.exists():
            logger.warning("ffmpeg failed for video: %s",
                           result.stderr.decode(errors="replace")[-300:])
            return ""
        if audio_path.stat().st_size == 0:
            return ""

        model = _get_model()
        if model is None:
            return ""

        segments, _info = model.transcribe(str(audio_path),
                                           beam_size=5,
                                           vad_filter=True)
        parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return "\n".join(parts)


def transcribe_audio(content: bytes, timeout: int = 600) -> str:
    """Transcribe an audio-only file (mp3/wav/m4a) directly.

    Used by the SharePoint connector for audio files; videos route
    through transcribe_video (ffmpeg first). Whisper accepts common
    audio containers natively.
    """
    if not WHISPER_AVAILABLE:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        audio_path = Path(tmp) / ("input" + _probe_ext(content))
        audio_path.write_bytes(content)
        model = _get_model()
        if model is None:
            return ""
        try:
            segments, _info = model.transcribe(str(audio_path),
                                               beam_size=5,
                                               vad_filter=True)
        except Exception as exc:  # unknown container
            logger.warning("Audio transcription failed: %s", exc)
            return ""
        parts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return "\n".join(parts)


def _probe_ext(content: bytes) -> str:
    """Cheap extension guess for the temp file Whisper reads.

    Whisper sniffs the container from bytes, so this is just a hint.
    """
    if content[:3] == b"ID3":
        return ".mp3"
    if content[:4] == b"fLaC":
        return ".flac"
    return ".wav"
