from __future__ import annotations

import importlib
import io
import logging
import math
import struct
import wave

from shea.audio.errors import AudioCaptureError, AudioConfigurationError
from shea.audio.ports import AudioSource

logger = logging.getLogger(__name__)


class MicrophoneAudioSource(AudioSource):
    """Capture mono 16 kHz PCM from the default microphone until silence."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        block_duration: float = 0.05,
        silence_duration: float = 1.2,
        speech_threshold: int = 500,
        speech_start_timeout: float = 8.0,
        max_duration: float = 30.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if block_duration <= 0 or silence_duration <= 0:
            raise ValueError("block_duration and silence_duration must be positive")
        if speech_threshold < 0:
            raise ValueError("speech_threshold must not be negative")
        if speech_start_timeout <= 0 or max_duration <= 0:
            raise ValueError("speech_start_timeout and max_duration must be positive")

        self._sample_rate = sample_rate
        self._block_duration = block_duration
        self._silence_duration = silence_duration
        self._speech_threshold = speech_threshold
        self._speech_start_timeout = speech_start_timeout
        self._max_duration = max_duration
        self._ready_message_shown = False

    def record_until_silence(self) -> bytes:
        try:
            sounddevice = importlib.import_module("sounddevice")
        except ModuleNotFoundError as exc:
            if exc.name != "sounddevice":
                raise
            raise AudioConfigurationError(
                "Microphone capture requires sounddevice. Install the audio extra "
                "with `pip install -e '.[audio]'`."
            ) from exc

        blocksize = max(1, round(self._sample_rate * self._block_duration))
        silence_blocks_required = max(1, math.ceil(self._silence_duration / self._block_duration))
        start_blocks_allowed = max(1, math.ceil(self._speech_start_timeout / self._block_duration))
        total_blocks_allowed = max(1, math.ceil(self._max_duration / self._block_duration))
        captured: list[bytes] = []
        speech_started = False
        silent_blocks = 0

        try:
            devices = sounddevice.query_devices()
            input_devices = [
                (index, device)
                for index, device in enumerate(devices)
                if device["max_input_channels"] > 0
            ]
            if not input_devices:
                raise AudioCaptureError(
                    "No microphone input device is available to this process. Connect or "
                    "enable a microphone, then check System Settings > Sound > Input. "
                    "On macOS, also enable microphone access for your terminal under "
                    "System Settings > Privacy & Security > Microphone, then restart it."
                )

            default_input = sounddevice.default.device[0]
            input_device = (
                default_input
                if isinstance(default_input, int)
                and any(index == default_input for index, _ in input_devices)
                else input_devices[0][0]
            )
            with sounddevice.RawInputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
                device=input_device,
            ) as stream:
                if not self._ready_message_shown:
                    print("Microphone ready. Say the wake phrase and your request.")
                    self._ready_message_shown = True
                for block_index in range(total_blocks_allowed):
                    frames, overflowed = stream.read(blocksize)
                    if overflowed:
                        raise AudioCaptureError(
                            "Microphone input overflowed; audio capture was incomplete."
                        )

                    if self._rms_level(frames) >= self._speech_threshold:
                        speech_started = True
                        silent_blocks = 0
                        captured.append(frames)
                    elif speech_started:
                        captured.append(frames)
                        silent_blocks += 1
                        if silent_blocks >= silence_blocks_required:
                            break
                    elif block_index + 1 >= start_blocks_allowed:
                        raise AudioCaptureError(
                            "No speech detected before the microphone start timeout."
                        )
        except AudioCaptureError:
            raise
        except Exception as exc:
            raise AudioCaptureError(
                "Could not open or read the microphone. Check System Settings > Sound > "
                "Input and, on macOS, System Settings > Privacy & Security > Microphone. "
                f"Then restart your terminal. Details: {exc}"
            ) from exc

        if not speech_started:
            raise AudioCaptureError(f"No speech detected within {self._max_duration:g} seconds.")

        output = io.BytesIO()
        try:
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self._sample_rate)
                wav.writeframes(b"".join(captured))
        except (wave.Error, OSError) as exc:
            raise AudioCaptureError(f"Could not encode captured audio: {exc}") from exc
        return output.getvalue()

    @staticmethod
    def _rms_level(frames: bytes) -> float:
        sample_count = len(frames) // 2
        if sample_count == 0:
            return 0.0
        samples = struct.unpack(f"<{sample_count}h", frames[: sample_count * 2])
        return math.sqrt(sum(sample * sample for sample in samples) / sample_count)
