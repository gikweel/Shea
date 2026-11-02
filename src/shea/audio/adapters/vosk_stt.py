from __future__ import annotations

import importlib
import io
import json
import os
import wave
from pathlib import Path
from typing import Any, cast

from shea.audio.errors import (
    AudioConfigurationError,
    AudioTranscriptionError,
)
from shea.audio.ports import AudioTranscriber


class VoskTranscriber(AudioTranscriber):
    """Offline speech recognition using a locally installed Vosk model."""

    def __init__(self, model_path: str | Path | None = None) -> None:
        configured_path = model_path or os.environ.get("SHEA_VOSK_MODEL") or None
        if configured_path is None:
            raise AudioConfigurationError(
                "Set SHEA_VOSK_MODEL to the directory containing a downloaded Vosk model."
            )

        self._model_path = Path(configured_path).expanduser()
        if not self._model_path.is_dir():
            raise AudioConfigurationError(
                f"Vosk model directory does not exist: {self._model_path}"
            )

        try:
            vosk = importlib.import_module("vosk")
        except ModuleNotFoundError as exc:
            if exc.name != "vosk":
                raise
            raise AudioConfigurationError(
                "Vosk is not installed. Install Shea's optional audio dependencies "
                "with `pip install -e '.[audio]'`."
            ) from exc

        try:
            self._model = vosk.Model(str(self._model_path))
            self._recognizer_type = vosk.KaldiRecognizer
        except Exception as exc:
            raise AudioConfigurationError(
                f"Could not load Vosk model from {self._model_path}: {exc}"
            ) from exc

    def transcribe(self, audio_data: bytes) -> str:
        if not audio_data:
            return ""

        try:
            with wave.open(io.BytesIO(audio_data), "rb") as audio:
                if audio.getcomptype() != "NONE":
                    raise AudioTranscriptionError("Vosk requires uncompressed PCM WAV audio.")
                if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
                    raise AudioTranscriptionError("Vosk requires mono, 16-bit PCM WAV audio.")

                recognizer = self._recognizer_type(self._model, audio.getframerate())
                segments: list[str] = []
                while frames := audio.readframes(4000):
                    if recognizer.AcceptWaveform(frames):
                        segment = self._result_text(recognizer.Result())
                        if segment:
                            segments.append(segment)
                final_segment = self._result_text(recognizer.FinalResult())
                if final_segment:
                    segments.append(final_segment)
        except AudioTranscriptionError:
            raise
        except (wave.Error, EOFError, json.JSONDecodeError, OSError) as exc:
            raise AudioTranscriptionError(f"Could not decode audio for Vosk: {exc}") from exc
        except Exception as exc:
            raise AudioTranscriptionError(f"Vosk transcription failed: {exc}") from exc

        return " ".join(segments)

    @staticmethod
    def _result_text(result_json: str) -> str:
        try:
            result: Any = json.loads(result_json)
        except json.JSONDecodeError as exc:
            raise AudioTranscriptionError(f"Vosk returned invalid JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise AudioTranscriptionError("Vosk returned an invalid transcript result.")
        text: object = cast(dict[str, object], result).get("text")
        if not isinstance(text, str):
            raise AudioTranscriptionError("Vosk returned a result without transcript text.")
        return text.strip()