from __future__ import annotations

import builtins
import sys
from types import ModuleType

import pytest

from shea import __main__ as shea_main


def test_desktop_command_launches_app_with_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, str | None, str | None]] = []
    desktop_module = ModuleType("shea.gui.desktop")
    desktop_module.start_desktop_app = lambda host, port, **paths: calls.append(
        (host, port, paths["db_path"], paths["workspace"])
    )
    monkeypatch.setitem(sys.modules, "shea.gui.desktop", desktop_module)

    shea_main.main(["desktop", "--host", "0.0.0.0", "--port", "8123"])

    assert calls == [("0.0.0.0", 8123, None, None)]


def test_desktop_command_explains_when_app_dependencies_are_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_import = builtins.__import__

    def missing_desktop_module(name: str, *args: object, **kwargs: object) -> ModuleType:
        if name == "shea.gui.desktop":
            raise ModuleNotFoundError("No module named 'webview'", name="webview")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", missing_desktop_module)

    with pytest.raises(SystemExit) as exc_info:
        shea_main.main(["desktop"])

    assert exc_info.value.code == 2
    assert "without `--cli-only`" in capsys.readouterr().err
