from __future__ import annotations

import os
from pathlib import Path

import pytest

from shea.gui.desktop import _configure_data_paths


def test_desktop_uses_configured_user_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_directory = tmp_path / "shea-data"
    monkeypatch.setenv("SHEA_DATA_DIR", str(data_directory))
    monkeypatch.delenv("SHEA_DB_PATH", raising=False)
    monkeypatch.delenv("SHEA_WORKSPACE", raising=False)

    _configure_data_paths(None, None)

    assert Path(os.environ["SHEA_DB_PATH"]) == data_directory / "shea.db"
    assert Path(os.environ["SHEA_WORKSPACE"]) == data_directory / "workspace"
    assert (data_directory / "workspace").is_dir()


def test_desktop_preserves_explicit_database_and_workspace_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "custom" / "shea.db"
    workspace = tmp_path / "custom-workspace"
    monkeypatch.delenv("SHEA_DB_PATH", raising=False)
    monkeypatch.delenv("SHEA_WORKSPACE", raising=False)

    _configure_data_paths(str(db_path), str(workspace))

    assert Path(os.environ["SHEA_DB_PATH"]) == db_path
    assert Path(os.environ["SHEA_WORKSPACE"]) == workspace
    assert db_path.parent.is_dir()
    assert workspace.is_dir()
