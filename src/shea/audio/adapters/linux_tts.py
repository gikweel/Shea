from __future__ import annotations

import logging
import subprocess

from shea.audio.errors import AudioOutputError
from shea.audio.ports import AudioSynthesizer

logger = logging.getLogger(__name__)


class LinuxAudioSynthesizer(AudioSynthesizer):
    """Text-to-Speech using Speech Dispatcher or eSpeak."""

    def synthesize(self, text: str) -> None:
        if not text:
            return

        logger.info("Synthesizing speech via Linux native TTS")
        try:
            subprocess.run(["spd-say", "-w", text], check=True, timeout=120)
            return
        except FileNotFoundError:
            pass
        except subprocess.SubprocessError as exc:
            raise AudioOutputError(f"Speech Dispatcher failed: {exc}") from exc

        try:
            subprocess.run(["espeak", text], check=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AudioOutputError(f"Linux speech synthesis failed: {exc}") from exc