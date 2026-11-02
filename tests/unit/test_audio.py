from __future__ import annotations

import io
import struct
import sys
import types
import wave
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from shea.audio.activation import ActivationController, ActivationState
from shea.audio.adapters.ingress import MicrophoneAudioSource
from shea.audio.adapters.linux_tts import LinuxAudioSynthesizer
from shea.audio.adapters.mac_tts import MacOsAudioSynthesizer
from shea.audio.adapters.vosk_stt import VoskTranscriber
from shea.audio.adapters.windows_tts import WindowsAudioSynthesizer
from shea.audio.errors import (
    AudioCaptureError,
    AudioConfigurationError,
    AudioOutputError,
    AudioTranscriptionError,
)
from shea.audio.service import AudioInteractionService
from shea.contracts.models import ToolRequest
from shea.tools.builtin.audio import register_audio_tools
from shea.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from shea.bootstrap import SheaRuntime


class _AudioSource:
    def __init__(self, data: bytes = b"audio") -> None:
        self.data = data

    def record_until_silence(self) -> bytes:
        return self.data


class _Transcriber:
    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe(self, audio_data: bytes) -> str:
        return self.text


def _wav_bytes(*, channels: int = 1, sample_width: int = 2) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00" * channels * sample_width * 4_000)
    return output.getvalue()


def _pcm_block(level: int, sample_count: int = 10) -> bytes:
    return struct.pack(f"<{sample_count}h", *([level] * sample_count))


def _fake_sounddevice(
    monkeypatch: pytest.MonkeyPatch,
    blocks: list[tuple[bytes, bool]],
    *,
    devices: list[dict[str, int]] | None = None,
    default_input: int = 0,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class InputStream:
        def __enter__(self) -> InputStream:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, frames: int) -> tuple[bytes, bool]:
            del frames
            if not blocks:
                raise AssertionError("microphone source read more blocks than expected")
            return blocks.pop(0)

    module = types.ModuleType("sounddevice")
    available_devices = devices if devices is not None else [{"max_input_channels": 1}]
    module.query_devices = lambda: available_devices  # type: ignore[attr-defined]
    module.default = types.SimpleNamespace(device=(default_input, 0))  # type: ignore[attr-defined]

    def raw_input_stream(**kwargs: object) -> InputStream:
        calls.append(kwargs)
        return InputStream()

    module.RawInputStream = raw_input_stream  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return calls


def _fake_vosk_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_result: str = '{"text": "hello"}',
    accept_waveform: Callable[[bytes], bool] | None = None,
) -> list[bytes]:
    model_paths: list[str] = []
    frames: list[bytes] = []

    class Model:
        def __init__(self, path: str) -> None:
            model_paths.append(path)

    class Recognizer:
        def __init__(self, model: object, sample_rate: int) -> None:
            assert sample_rate == 16_000

        def AcceptWaveform(self, data: bytes) -> bool:
            frames.append(data)
            return accept_waveform(data) if accept_waveform else True

        def Result(self) -> str:
            return '{"text": "recognized"}'

        def FinalResult(self) -> str:
            return final_result

    monkeypatch.setitem(
        sys.modules,
        "vosk",
        types.ModuleType("vosk"),
    )
    module = sys.modules["vosk"]
    module.Model = Model  # type: ignore[attr-defined]
    module.KaldiRecognizer = Recognizer  # type: ignore[attr-defined]
    return frames


def test_vosk_transcriber_decodes_local_wav(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_vosk_module(monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    transcriber = VoskTranscriber(model_dir)

    assert transcriber.transcribe(_wav_bytes()) == "recognized hello"


def test_vosk_transcriber_handles_empty_audio_without_loading_recognizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    frames = _fake_vosk_module(monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    assert VoskTranscriber(model_dir).transcribe(b"") == ""
    assert frames == []


@pytest.mark.parametrize(
    ("channels", "sample_width"),
    [(2, 2), (1, 1), (1, 4)],
)
def test_vosk_transcriber_rejects_non_mono_16_bit_audio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    channels: int,
    sample_width: int,
) -> None:
    _fake_vosk_module(monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    with pytest.raises(AudioTranscriptionError, match="mono, 16-bit"):
        VoskTranscriber(model_dir).transcribe(
            _wav_bytes(channels=channels, sample_width=sample_width)
        )


def test_vosk_transcriber_reports_missing_model_directory(tmp_path: Path) -> None:
    with pytest.raises(AudioConfigurationError, match="model directory does not exist"):
        VoskTranscriber(tmp_path / "missing")


def test_vosk_transcriber_reports_missing_optional_dependency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    def import_missing(name: str) -> Any:
        assert name == "vosk"
        raise ModuleNotFoundError("missing vosk", name="vosk")

    monkeypatch.setattr("shea.audio.adapters.vosk_stt.importlib.import_module", import_missing)

    with pytest.raises(AudioConfigurationError, match="Vosk is not installed"):
        VoskTranscriber(model_dir)


def test_vosk_transcriber_rejects_invalid_wav(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_vosk_module(monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    with pytest.raises(AudioTranscriptionError, match="Could not decode"):
        VoskTranscriber(model_dir).transcribe(b"not a wav")


def test_vosk_transcriber_rejects_malformed_result_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_vosk_module(monkeypatch, final_result="[]")
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    with pytest.raises(AudioTranscriptionError, match="invalid transcript result"):
        VoskTranscriber(model_dir).transcribe(_wav_bytes())


def test_activation_dispatches_only_text_after_wake_word(
    capsys: pytest.CaptureFixture[str],
) -> None:
    intents: list[str] = []
    controller = ActivationController(
        source=_AudioSource(),
        transcriber=_Transcriber("Hey Shea, write a note."),
        on_intent=intents.append,
        wake_word="hey shea",
    )

    assert controller.run_once() == "write a note"
    assert intents == ["write a note"]
    assert controller.state is ActivationState.IDLE
    output = capsys.readouterr().out
    assert "Listening for 'hey shea'." in output
    assert "Heard: Hey Shea, write a note." in output


def test_activation_does_not_dispatch_without_wake_word(
    capsys: pytest.CaptureFixture[str],
) -> None:
    intents: list[str] = []
    controller = ActivationController(
        source=_AudioSource(),
        transcriber=_Transcriber("write a note"),
        on_intent=intents.append,
        wake_word="hey shea",
    )

    assert controller.run_once() is None
    assert intents == []
    assert controller.state is ActivationState.IDLE
    output = capsys.readouterr().out
    assert "Heard: write a note" in output
    assert "Wake phrase 'hey shea' not detected" in output


def test_activation_speaks_reply_inside_speaking_state() -> None:
    class Synthesizer:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        def synthesize(self, text: str) -> None:
            self.spoken.append(text)

    synthesizer = Synthesizer()
    controller = ActivationController(
        source=_AudioSource(),
        transcriber=_Transcriber("hello"),
        on_intent=lambda _: "Hi there.",
        synthesizer=synthesizer,
    )

    assert controller.run_once() == "hello"
    assert synthesizer.spoken == ["Hi there."]
    assert controller.state is ActivationState.IDLE


def test_activation_recovers_state_after_transcription_failure() -> None:
    class FailingTranscriber:
        def transcribe(self, audio_data: bytes) -> str:
            raise AudioTranscriptionError("bad audio")

    controller = ActivationController(
        source=_AudioSource(),
        transcriber=FailingTranscriber(),
        on_intent=lambda _: None,
    )

    with pytest.raises(AudioTranscriptionError, match="bad audio"):
        controller.run_once()

    assert controller.state is ActivationState.IDLE


def test_microphone_source_records_until_silence_and_encodes_pcm_wav(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_sounddevice(
        monkeypatch,
        [
            (_pcm_block(0), False),
            (_pcm_block(1200), False),
            (_pcm_block(900), False),
            (_pcm_block(0), False),
            (_pcm_block(0), False),
        ],
    )
    source = MicrophoneAudioSource(
        sample_rate=1000,
        block_duration=0.01,
        silence_duration=0.02,
        speech_threshold=500,
        speech_start_timeout=0.02,
        max_duration=1,
    )

    captured = source.record_until_silence()

    with wave.open(io.BytesIO(captured), "rb") as wav:
        assert wav.getframerate() == 1000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 40
        assert calls == [
            {
                "samplerate": 1000,
                "channels": 1,
                "dtype": "int16",
                "blocksize": 10,
                "device": 0,
            }
        ]


def test_microphone_source_shows_ready_message_only_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_sounddevice(
        monkeypatch,
        [
            (_pcm_block(1200), False),
            (_pcm_block(0), False),
            (_pcm_block(1200), False),
            (_pcm_block(0), False),
        ],
    )
    source = MicrophoneAudioSource(
        sample_rate=1000,
        block_duration=0.01,
        silence_duration=0.01,
        speech_threshold=500,
        speech_start_timeout=0.02,
        max_duration=1,
    )

    source.record_until_silence()
    source.record_until_silence()

    assert capsys.readouterr().out.count("Microphone ready.") == 1


def test_microphone_source_errors_when_speech_never_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sounddevice(
        monkeypatch,
        [(_pcm_block(0), False), (_pcm_block(0), False)],
    )
    source = MicrophoneAudioSource(
        sample_rate=1000,
        block_duration=0.01,
        silence_duration=0.02,
        speech_start_timeout=0.02,
        max_duration=1,
    )

    with pytest.raises(AudioCaptureError, match="No speech detected"):
        source.record_until_silence()


def test_microphone_source_reports_input_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sounddevice(monkeypatch, [(_pcm_block(1000), True)])
    source = MicrophoneAudioSource(
        sample_rate=1000,
        block_duration=0.01,
        silence_duration=0.02,
        speech_start_timeout=0.02,
        max_duration=1,
    )

    with pytest.raises(AudioCaptureError, match="overflowed"):
        source.record_until_silence()


def test_microphone_source_explains_when_no_input_device_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sounddevice(monkeypatch, [], devices=[])

    with pytest.raises(AudioCaptureError, match="No microphone input device"):
        MicrophoneAudioSource().record_until_silence()


def test_microphone_source_explains_when_macOS_denies_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_sounddevice(monkeypatch, [])

    class DeniedInputStream:
        def __enter__(self) -> DeniedInputStream:
            raise RuntimeError("Unanticipated host error")

        def __exit__(self, *args: object) -> None:
            return None

    def denied_stream(**kwargs: object) -> DeniedInputStream:
        calls.append(kwargs)
        return DeniedInputStream()

    monkeypatch.setattr(
        "shea.audio.adapters.ingress.importlib.import_module",
        lambda _: types.SimpleNamespace(
            query_devices=lambda: [{"max_input_channels": 1}],
            default=types.SimpleNamespace(device=(0, 0)),
            RawInputStream=denied_stream,
        ),
    )

    with pytest.raises(AudioCaptureError, match="Privacy & Security > Microphone"):
        MicrophoneAudioSource().record_until_silence()


def test_microphone_source_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        MicrophoneAudioSource(speech_start_timeout=0)


def test_microphone_source_reports_missing_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def import_missing(name: str) -> Any:
        assert name == "sounddevice"
        raise ModuleNotFoundError("missing sounddevice", name="sounddevice")

    monkeypatch.setattr("shea.audio.adapters.ingress.importlib.import_module", import_missing)

    with pytest.raises(AudioConfigurationError, match="Install the audio extra"):
        MicrophoneAudioSource().record_until_silence()


def test_macos_synthesizer_uses_argument_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> None:
        calls.append((command, kwargs))

    monkeypatch.setattr("shea.audio.adapters.mac_tts.subprocess.run", run)

    MacOsAudioSynthesizer().synthesize("Hi there")

    assert calls == [(["say", "Hi there"], {"check": True, "timeout": 120})]


def test_macos_synthesizer_surfaces_missing_native_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **kwargs: object) -> None:
        del command, kwargs
        raise FileNotFoundError("say")

    monkeypatch.setattr("shea.audio.adapters.mac_tts.subprocess.run", run)

    with pytest.raises(AudioOutputError, match="macOS speech synthesis failed"):
        MacOsAudioSynthesizer().synthesize("Hi")


def test_linux_synthesizer_falls_back_to_espeak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> None:
        del kwargs
        commands.append(command)
        if command[0] == "spd-say":
            raise FileNotFoundError

    monkeypatch.setattr("shea.audio.adapters.linux_tts.subprocess.run", run)

    LinuxAudioSynthesizer().synthesize("Hello")

    assert commands == [["spd-say", "-w", "Hello"], ["espeak", "Hello"]]


def test_windows_synthesizer_escapes_text_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> None:
        commands.append((command, kwargs))

    monkeypatch.setattr("shea.audio.adapters.windows_tts.subprocess.run", run)

    WindowsAudioSynthesizer().synthesize("it's safe")

    assert commands == [
        (
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Add-Type -AssemblyName System.Speech; "
                "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('it''s safe')",
            ],
            {"check": True, "timeout": 120},
        )
    ]


def test_audio_speak_tool_registers_and_uses_synthesizer() -> None:
    class Synthesizer:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        def synthesize(self, text: str) -> None:
            self.spoken.append(text)

    registry = ToolRegistry()
    synthesizer = Synthesizer()
    register_audio_tools(registry, synthesizer)

    declaration = registry.get_declaration("audio.speak")
    response = registry.get_handler("audio.speak")(
        ToolRequest(
            request_id="r1", tool="audio.speak", action="speak", arguments={"text": "hello"}
        )
    )
    invalid = registry.get_handler("audio.speak")(
        ToolRequest(request_id="r2", tool="audio.speak", action="speak", arguments={"text": "  "})
    )

    assert declaration.capabilities == frozenset({"audio.speak"})
    assert response.success is True
    assert synthesizer.spoken == ["hello"]
    assert invalid.success is False


def test_audio_service_dispatches_wake_word_request_and_speaks_reply() -> None:
    class InteractionService:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def handle_text(self, **kwargs: object) -> types.SimpleNamespace:
            self.requests.append(kwargs)
            return types.SimpleNamespace(message="Hello there.", error=None, task=None)

    class Synthesizer:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        def synthesize(self, text: str) -> None:
            self.spoken.append(text)

    interaction = InteractionService()
    runtime = types.SimpleNamespace(interaction_service=interaction)
    synthesizer = Synthesizer()
    service = AudioInteractionService(
        cast("SheaRuntime", runtime),
        source=_AudioSource(),
        transcriber=_Transcriber("Hey Shea, say hello."),
        synthesizer=synthesizer,
    )

    assert service.listen_and_dispatch(service._handle_intent) == "say hello"
    assert interaction.requests == [
        {
            "text": "say hello",
            "session_id": "audio_session",
            "actor": "local_user",
            "explicit_user_ack": False,
        }
    ]
    assert synthesizer.spoken == ["Hello there."]
