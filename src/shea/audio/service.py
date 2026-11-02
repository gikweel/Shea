from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

from shea.audio.activation import ActivationController
from shea.audio.adapters import VoskTranscriber, get_default_audio_synthesizer
from shea.audio.adapters.ingress import MicrophoneAudioSource
from shea.audio.ports import (
    AudioSource,
    AudioSynthesizer,
    AudioTranscriber,
    WakeWordDetector,
)
from shea.contracts.enums import TaskState

if TYPE_CHECKING:
    from shea.bootstrap import SheaRuntime

logger = logging.getLogger(__name__)


class AudioInteractionService:
    """Runs the offline voice input → interaction → speech output loop."""

    def __init__(
        self,
        runtime: SheaRuntime,
        detector: WakeWordDetector | None = None,
        source: AudioSource | None = None,
        transcriber: AudioTranscriber | None = None,
        synthesizer: AudioSynthesizer | None = None,
        wake_word: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.detector = detector
        self.source = source if source is not None else MicrophoneAudioSource()
        self.transcriber = transcriber if transcriber is not None else VoskTranscriber()
        self.synthesizer = (
            synthesizer if synthesizer is not None else get_default_audio_synthesizer()
        )
        self.wake_word = (
            wake_word if wake_word is not None else os.environ.get("SHEA_WAKE_WORD", "hey shea")
        )

    def start_loop(self) -> None:
        """Listen for utterances until interrupted; only dispatch wake-word matches."""
        logger.info("Offline audio interaction loop started.")
        try:
            while True:
                if self.detector is not None and not self.detector.listen_for_wake_word():
                    continue
                self.listen_and_dispatch(self._handle_intent)
        except KeyboardInterrupt:
            logger.info("Audio loop terminated by user.")

    def listen_and_dispatch(self, on_intent: Callable[[str], str | None]) -> str | None:
        controller = ActivationController(
            source=self.source,
            transcriber=self.transcriber,
            on_intent=on_intent,
            synthesizer=self.synthesizer,
            wake_word=self.wake_word,
        )
        return controller.run_once()

    def _handle_intent(self, text: str) -> str | None:
        result = self.runtime.interaction_service.handle_text(
            text=text,
            session_id="audio_session",
            actor="local_user",
            explicit_user_ack=False,
        )

        if result.message:
            reply = result.message
        elif result.error == "authorization_required":
            reply = "I need your confirmation before I can do that."
        elif result.error:
            logger.error("Audio request failed: %s", result.error)
            reply = "I couldn't complete that request."
        elif result.task is not None and result.task.state is TaskState.COMPLETED:
            reply = "Done."
        elif result.task is not None:
            reply = f"Your request finished with status {result.task.state.value}."
        else:
            return None

        return reply
