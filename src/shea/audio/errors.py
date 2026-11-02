from __future__ import annotations


class AudioError(RuntimeError):
    """Base error for audio input, transcription, and output failures."""


class AudioConfigurationError(AudioError):
    """The selected audio backend is not installed or configured."""


class AudioCaptureError(AudioError):
    """Audio could not be captured from the configured input device."""


class AudioTranscriptionError(AudioError):
    """Audio could not be transcribed."""


class AudioOutputError(AudioError):
    """Speech could not be played through the configured output device."""
