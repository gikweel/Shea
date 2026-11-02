from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from shea.cli.main import cli


def test_listen_command_is_registered_in_click_cli() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "listen" in result.output


def test_chat_loads_interface_saved_model_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[bool] = []
    runtime = SimpleNamespace(
        model_settings_service=SimpleNamespace(load_saved=lambda: loaded.append(True)),
    )
    monkeypatch.setattr("shea.cli.main.build_runtime", lambda **_: runtime)
    monkeypatch.setattr("builtins.input", lambda _: "exit")

    result = CliRunner().invoke(cli, ["chat"])

    assert result.exit_code == 0
    assert loaded == [True]


def test_listen_loads_interface_saved_model_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[bool] = []
    runtime = SimpleNamespace(
        model_settings_service=SimpleNamespace(load_saved=lambda: loaded.append(True)),
    )

    class AudioInteractionService:
        def __init__(self, value: object) -> None:
            assert value is runtime

        def start_loop(self) -> None:
            pass

    monkeypatch.setattr("shea.cli.main.build_runtime", lambda **_: runtime)
    monkeypatch.setattr("shea.cli.main.AudioInteractionService", AudioInteractionService)

    result = CliRunner().invoke(cli, ["listen"])

    assert result.exit_code == 0
    assert loaded == [True]
