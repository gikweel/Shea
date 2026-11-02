from __future__ import annotations

import multiprocessing
import os
import sys
import time
from pathlib import Path

import uvicorn
import webview

from shea.api.server import create_app


def _default_data_directory() -> Path:
    configured_directory = os.environ.get("SHEA_DATA_DIR")
    if configured_directory:
        return Path(configured_directory).expanduser()
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "Shea"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Shea"
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "shea"


def _configure_data_paths(db_path: str | None, workspace: str | None) -> None:
    data_directory = _default_data_directory()
    database = Path(db_path).expanduser() if db_path else data_directory / "shea.db"
    workspace_directory = (
        Path(workspace).expanduser() if workspace else data_directory / "workspace"
    )
    os.environ.setdefault("SHEA_DB_PATH", str(database))
    os.environ.setdefault("SHEA_WORKSPACE", str(workspace_directory))
    Path(os.environ["SHEA_DB_PATH"]).expanduser().parent.mkdir(parents=True, exist_ok=True)
    Path(os.environ["SHEA_WORKSPACE"]).expanduser().mkdir(parents=True, exist_ok=True)


def run_api_server(host: str, port: int) -> None:
    """Run the FastAPI server in a separate process."""
    uvicorn.run(create_app, host=host, port=port, log_level="error")


def start_desktop_app(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    db_path: str | None = None,
    workspace: str | None = None,
) -> None:
    """Launch the native desktop window and the backend server."""
    _configure_data_paths(db_path, workspace)

    # Ensure multiprocessing works nicely on macOS/Windows
    if sys.platform.startswith("darwin") or sys.platform.startswith("win"):
        multiprocessing.set_start_method("spawn", force=True)

    server_process = multiprocessing.Process(
        target=run_api_server, args=(host, port), daemon=True
    )
    server_process.start()

    # Give the server a moment to bind to the port
    time.sleep(1.0)

    url = f"http://{host}:{port}/"
    
    # Create the native window and confirm it was initialized before starting the loop.
    window = webview.create_window(  # pyright: ignore[reportUnknownMemberType]
        title="Shea Agent",
        url=url,
        width=1024,
        height=768,
        min_size=(800, 600),
        text_select=True,
    )
    if window is None:
        raise RuntimeError("Failed to create the Shea desktop window.")

    try:
        # Start the native event loop
        webview.start(debug=False)
    finally:
        # Ensure server is torn down when the window closes
        server_process.terminate()
        server_process.join()


if __name__ == "__main__":
    start_desktop_app()