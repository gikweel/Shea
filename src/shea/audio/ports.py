from __future__ import annotations

from typing import Protocol


class WakeWordDetector(Protocol):
    """Detects a specific wake word in a continuous audio stream."""

    def listen_for_wake_word(self) -> bool:
        """Blocks until the wake word is detected."""
        ...


class AudioTranscriber(Protocol):
    """Transcribes an audio buffer into text."""

    def transcribe(self, audio_data: bytes) -> str:
        """Synchronously transcribes the provided audio buffer."""
        ...


class AudioSource(Protocol):
    """Provides a stream of audio data."""

    def record_until_silence(self) -> bytes:
        """Records from the input source until a period of silence is detected."""
        ...


class AudioSynthesizer(Protocol):
    """Converts text to speech (TTS)."""

    def synthesize(self, text: str) -> None:
        """Synchronously plays the synthesized audio."""
        ...
