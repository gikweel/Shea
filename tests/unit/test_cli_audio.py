from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shea import __main__ as shea_main


def test_listen_command_is_registered(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        shea_main.main(["--help"])

    assert exc_info.value.code == 0
    assert "listen" in capsys.readouterr().out


def test_listen_command_starts_audio_service_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    loaded: list[bool] = []
    runtime = SimpleNamespace(
        conn=connection,
        model_settings_service=SimpleNamespace(load_saved=lambda: loaded.append(True)),
    )
    build_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def build_runtime(*args: Any, **kwargs: Any) -> Any:
        build_calls.append((args, kwargs))
        return runtime

    started: list[object] = []

    class AudioInteractionService:
        def __init__(self, value: object) -> None:
            started.append(value)

        def start_loop(self) -> None:
            pass

    monkeypatch.setattr(shea_main, "build_runtime", build_runtime)
    monkeypatch.setattr("shea.audio.service.AudioInteractionService", AudioInteractionService)

    shea_main.main(["--db", "voice.db", "--workspace", str(tmp_path), "listen"])

    assert build_calls == [
        (
            ("voice.db",),
            {"workspace": str(tmp_path), "register_builtins": True},
        )
    ]
    assert started == [runtime]
    assert loaded == [True]
    assert connection.closed is True


def test_run_command_loads_interface_saved_model_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loaded: list[bool] = []
    runtime = SimpleNamespace(
        model_settings_service=SimpleNamespace(load_saved=lambda: loaded.append(True)),
    )

    class SheaApp:
        def __init__(self, value: object) -> None:
            assert value is runtime

        def chat(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(
                task=None,
                message="Ready",
                error=None,
                planning=None,
                run=None,
            )

    monkeypatch.setattr(shea_main, "build_runtime", lambda *_, **__: runtime)
    monkeypatch.setattr(shea_main, "SheaApp", SheaApp)

    shea_main.main(["--workspace", str(tmp_path), "run", "hello"])

    assert loaded == [True]
