from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from shea import __main__ as shea_main
from shea.system.updater import UpdateError, UpdateManifest, UpdateService


def _service(tmp_path: Path) -> UpdateService:
    return UpdateService(tmp_path, project_dir=None)


def _fake_wheel_builder(
    monkeypatch: pytest.MonkeyPatch,
    service: UpdateService,
    contents: bytes = b"wheel",
) -> None:
    def build_wheel(wheel_dir: Path) -> Path:
        wheel = wheel_dir / "shea-0.1.0-py3-none-any.whl"
        wheel.write_bytes(contents)
        return wheel

    monkeypatch.setattr(service, "_build_current_wheel", build_wheel)


def test_update_check_selects_newer_stable_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_fetch_releases",
        lambda: [
            {
                "draft": False,
                "prerelease": True,
                "tag_name": "v0.3.0-beta.1",
                "tarball_url": "https://api.github.com/repos/mwasgibson/Shea/tarball/v0.3.0-beta.1",
            },
            {
                "draft": False,
                "prerelease": False,
                "tag_name": "v0.2.0",
                "tarball_url": "https://api.github.com/repos/mwasgibson/Shea/tarball/v0.2.0",
                "body": "Release notes",
            },
        ],
    )

    manifest = service.check_for_updates("0.1.0")

    assert manifest is not None
    assert manifest.version == "0.2.0"
    assert manifest.tag_name == "v0.2.0"
    assert manifest.changelog == "Release notes"
    assert manifest.channel == "stable"


def test_update_check_noops_when_no_new_release_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_fetch_releases", lambda: [])

    assert service.check_for_updates("0.1.0") is None
    assert not service.snapshots_dir.exists()


def test_update_check_rejects_untrusted_release_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_fetch_releases",
        lambda: [
            {
                "tag_name": "v0.2.0",
                "tarball_url": "https://example.com/shea.tar.gz",
            }
        ],
    )

    with pytest.raises(UpdateError, match="configured Shea GitHub repo"):
        service.check_for_updates("0.1.0")


def test_snapshot_uses_sqlite_backup_api_and_lists_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _fake_wheel_builder(monkeypatch, service)
    connection = sqlite3.connect(service.db_path)
    connection.execute("CREATE TABLE state (value TEXT)")
    connection.execute("INSERT INTO state VALUES ('before')")
    connection.commit()
    connection.close()

    config = service.config_path
    config.write_text('{"value":"before"}')
    snapshot = service.create_snapshot()

    snapshot_conn = sqlite3.connect(snapshot / "shea.db")
    assert snapshot_conn.execute("SELECT value FROM state").fetchone() == ("before",)
    snapshot_conn.close()
    assert json.loads((snapshot / "config.json").read_text()) == {"value": "before"}
    assert [item.id for item in service.list_snapshots()] == [snapshot.name]


def test_current_checkout_builds_a_rollback_wheel(tmp_path: Path) -> None:
    project_dir = Path(__file__).resolve().parents[2]
    service = UpdateService(tmp_path, project_dir=project_dir)
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()

    wheel = service._build_current_wheel(wheel_dir)

    assert wheel.is_file()
    assert wheel.name.startswith("shea-0.1.0-")


def test_restore_replaces_code_and_config_but_preserves_database_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _fake_wheel_builder(monkeypatch, service, b"saved-wheel")
    connection = sqlite3.connect(service.db_path)
    connection.execute("CREATE TABLE state (value TEXT)")
    connection.execute("INSERT INTO state VALUES ('before')")
    connection.commit()
    connection.close()
    service.config_path.write_text('{"value":"before"}')
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=keep-me\n")

    snapshot = service.create_snapshot()
    service.config_path.write_text('{"value":"after"}')
    connection = sqlite3.connect(service.db_path)
    connection.execute("UPDATE state SET value='after'")
    connection.commit()
    connection.close()

    installed: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "_run_pip", lambda *args: installed.append(args))

    service.restore(snapshot.name)

    assert installed[0][0:2] == ("install", "--force-reinstall")
    assert Path(installed[0][2]).read_bytes() == b"saved-wheel"
    assert json.loads(service.config_path.read_text()) == {"value": "before"}
    assert env_file.read_text() == "TOKEN=keep-me\n"
    connection = sqlite3.connect(service.db_path)
    assert connection.execute("SELECT value FROM state").fetchone() == ("after",)
    connection.close()


def test_restore_rejects_corrupted_snapshot_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _fake_wheel_builder(monkeypatch, service)
    snapshot = service.create_snapshot()
    (snapshot / "shea-0.1.0-py3-none-any.whl").write_bytes(b"tampered")
    installed: list[tuple[str, ...]] = []
    monkeypatch.setattr(service, "_run_pip", lambda *args: installed.append(args))

    with pytest.raises(UpdateError, match="missing or corrupted"):
        service.restore(snapshot.name)
    assert installed == []


def test_failed_update_restores_snapshot_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    _fake_wheel_builder(monkeypatch, service, b"prior-wheel")
    calls: list[tuple[str, ...]] = []

    def run_pip(*args: str) -> None:
        calls.append(args)
        if args[0] == "install" and "https://" in args[-1]:
            raise UpdateError("simulated install failure")

    monkeypatch.setattr(service, "_run_pip", run_pip)
    update = UpdateManifest(
        version="0.2.0",
        channel="stable",
        requires_migration=False,
        changelog="",
        tag_name="v0.2.0",
        archive_url="https://api.github.com/repos/mwasgibson/Shea/tarball/v0.2.0",
    )

    assert service.apply_update(update) is False
    assert calls[0] == ("install", "--upgrade", update.archive_url)
    assert calls[1][0:2] == ("install", "--force-reinstall")
    assert Path(calls[1][2]).read_bytes() == b"prior-wheel"


def test_failed_restore_returns_to_the_pre_restore_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    wheel_contents = iter((b"target-wheel", b"current-wheel"))

    def build_wheel(wheel_dir: Path) -> Path:
        wheel = wheel_dir / "shea-0.1.0-py3-none-any.whl"
        wheel.write_bytes(next(wheel_contents))
        return wheel

    monkeypatch.setattr(service, "_build_current_wheel", build_wheel)
    service.config_path.write_text('{"value":"target"}')
    target_snapshot = service.create_snapshot()
    service.config_path.write_text('{"value":"current"}')
    calls: list[tuple[str, ...]] = []

    def run_pip(*args: str) -> None:
        calls.append(args)
        if len(calls) == 1:
            raise UpdateError("simulated restore failure")

    monkeypatch.setattr(service, "_run_pip", run_pip)

    with pytest.raises(UpdateError, match="prior installation was restored"):
        service.restore(target_snapshot.name)

    assert len(calls) == 2
    assert Path(calls[1][-1]).read_bytes() == b"current-wheel"
    assert json.loads(service.config_path.read_text()) == {"value": "current"}


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(UpdateError, match="Invalid snapshot ID"):
        service.restore("../shea.db")


@pytest.mark.parametrize("operation", ["snapshot", "restore", "update"])
def test_frozen_desktop_rejects_wheel_based_code_operations(
    operation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr("shea.system.updater.sys.frozen", True, raising=False)

    with pytest.raises(UpdateError, match="not supported by standalone desktop packages"):
        if operation == "snapshot":
            service.create_snapshot()
        elif operation == "restore":
            service.restore("snapshot_123")
        else:
            service.apply_update(
                UpdateManifest(
                    version="0.2.0",
                    channel="stable",
                    requires_migration=False,
                    changelog="",
                    tag_name="v0.2.0",
                    archive_url=(
                        "https://api.github.com/repos/mwasgibson/Shea/tarball/v0.2.0"
                    ),
                )
            )

    assert not service.snapshots_dir.exists()


def test_backup_cli_does_not_start_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    created: list[str] = []

    class BackupService:
        def __init__(self, data_dir: Path, *, db_path: Path) -> None:
            assert db_path == tmp_path / "shea.db"
            assert data_dir == tmp_path

        def create_snapshot(self) -> Path:
            created.append("snapshot_123")
            return tmp_path / created[-1]

    def unexpected_runtime(*args: object, **kwargs: object) -> object:
        raise AssertionError("backup command must not start the runtime")

    monkeypatch.setattr(shea_main, "UpdateService", BackupService)
    monkeypatch.setattr(shea_main, "build_runtime", unexpected_runtime)

    shea_main.main(
        [
            "--db",
            str(tmp_path / "shea.db"),
            "--workspace",
            str(tmp_path / "workspace"),
            "backup",
        ]
    )

    assert created == ["snapshot_123"]
    assert "Snapshot created: snapshot_123" in capsys.readouterr().out
