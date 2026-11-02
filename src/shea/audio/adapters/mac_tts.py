from __future__ import annotations

import logging
import subprocess

from shea.audio.errors import AudioOutputError
from shea.audio.ports import AudioSynthesizer

logger = logging.getLogger(__name__)


class MacOsAudioSynthesizer(AudioSynthesizer):
    """Text-to-Speech adapter using the native macOS `say` command."""

    def synthesize(self, text: str) -> None:
        if not text:
            return

        logger.info("Synthesizing speech via macOS 'say'")
        try:
            subprocess.run(["say", text], check=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AudioOutputError(f"macOS speech synthesis failed: {exc}") from exc