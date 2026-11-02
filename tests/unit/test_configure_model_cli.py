from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shea import __main__ as shea_main


class Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_configure_model_prompts_for_hidden_key_and_saves_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = Connection()
    saved: list[dict[str, str]] = []
    loaded: list[bool] = []
    runtime = SimpleNamespace(
        conn=connection,
        model_settings_service=SimpleNamespace(
            load_saved=lambda: loaded.append(True),
            save=lambda **values: (
                saved.append(values)
                or SimpleNamespace(provider=values["provider"], model_name=values["model_name"])
            ),
        ),
    )
    answers = iter(["", "", ""])
    monkeypatch.setattr(shea_main, "build_runtime", lambda *_, **__: runtime)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("shea.__main__.getpass.getpass", lambda _prompt: "private-key")

    shea_main.main(["--workspace", str(tmp_path), "configure-model"])

    assert loaded == [True]
    assert saved == [
        {
            "provider": "openai-compatible",
            "base_url": "https://api.openai.com/v1",
            "model_name": "gpt-4o-mini",
            "api_key": "private-key",
        }
    ]
    assert connection.closed is True
    output = capsys.readouterr().out
    assert "private-key" not in output
    assert "Configured openai-compatible model gpt-4o-mini" in output


def test_configure_model_skips_api_key_for_ollama(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    connection = Connection()
    saved: list[dict[str, Any]] = []
    runtime = SimpleNamespace(
        conn=connection,
        model_settings_service=SimpleNamespace(
            load_saved=lambda: None,
            save=lambda **values: (
                saved.append(values)
                or SimpleNamespace(provider=values["provider"], model_name=values["model_name"])
            ),
        ),
    )
    answers = iter(["ollama", "", ""])
    monkeypatch.setattr(shea_main, "build_runtime", lambda *_, **__: runtime)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr(
        "shea.__main__.getpass.getpass",
        lambda _prompt: pytest.fail("Ollama should not prompt for an API key"),
    )

    shea_main.main(["--workspace", str(tmp_path), "configure-model"])

    assert saved == [
        {
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "model_name": "llama3.2",
            "api_key": "",
        }
    ]
    assert connection.closed is True


def test_configure_model_reports_invalid_settings_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    connection = Connection()
    runtime = SimpleNamespace(
        conn=connection,
        model_settings_service=SimpleNamespace(
            load_saved=lambda: None,
            save=lambda **_: (_ for _ in ()).throw(ValueError("invalid URL")),
        ),
    )
    answers = iter(["", "", ""])
    monkeypatch.setattr(shea_main, "build_runtime", lambda *_, **__: runtime)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    monkeypatch.setattr("shea.__main__.getpass.getpass", lambda _prompt: "private-key")

    with pytest.raises(SystemExit) as exc_info:
        shea_main.main(["--workspace", str(tmp_path), "configure-model"])

    assert exc_info.value.code == 2
    assert connection.closed is True
    assert "Model configuration error: invalid URL" in capsys.readouterr().err
