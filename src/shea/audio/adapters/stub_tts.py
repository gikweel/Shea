from __future__ import annotations

import logging

from shea.audio.ports import AudioSynthesizer

logger = logging.getLogger(__name__)


class StubAudioSynthesizer(AudioSynthesizer):
    """Fallback adapter that just logs speech."""

    def synthesize(self, text: str) -> None:
        logger.info(f"[StubTTS] (No native TTS available) Agent says: {text}")