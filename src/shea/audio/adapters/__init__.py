from __future__ import annotations

import sys

from shea.audio.adapters.linux_tts import LinuxAudioSynthesizer
from shea.audio.adapters.mac_tts import MacOsAudioSynthesizer
from shea.audio.adapters.stub_tts import StubAudioSynthesizer
from shea.audio.adapters.vosk_stt import VoskTranscriber
from shea.audio.adapters.windows_tts import WindowsAudioSynthesizer
from shea.audio.ports import AudioSynthesizer


def get_default_audio_synthesizer() -> AudioSynthesizer:
    """Return the correct native TTS adapter for the current OS."""
    if sys.platform == "darwin":
        return MacOsAudioSynthesizer()
    if sys.platform.startswith("linux"):
        return LinuxAudioSynthesizer()
    if sys.platform == "win32":
        return WindowsAudioSynthesizer()
    return StubAudioSynthesizer()


__all__ = ["VoskTranscriber", "get_default_audio_synthesizer"]