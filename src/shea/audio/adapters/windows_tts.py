from __future__ import annotations

import logging
import subprocess

from shea.audio.errors import AudioOutputError
from shea.audio.ports import AudioSynthesizer

logger = logging.getLogger(__name__)


class WindowsAudioSynthesizer(AudioSynthesizer):
    """Text-to-Speech adapter using Windows native SAPI (via PowerShell)."""

    def synthesize(self, text: str) -> None:
        if not text:
            return

        logger.info("Synthesizing speech via Windows PowerShell System.Speech")
        escaped_text = text.replace("'", "''")
        ps_command = (
            "Add-Type -AssemblyName System.Speech; "
            "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak("
            f"'{escaped_text}')"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_command],
                check=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AudioOutputError(f"Windows speech synthesis failed: {exc}") from exc
