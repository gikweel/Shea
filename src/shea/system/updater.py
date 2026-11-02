from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast
from urllib.parse import urlparse

from shea import __version__

logger = logging.getLogger(__name__)

GITHUB_ARCHIVE_HOSTS = frozenset({"api.github.com", "github.com"})

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]


class UpdateError(Exception):
    """An update or restore could not be completed safely."""


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    channel: str
    requires_migration: bool
    changelog: str
    tag_name: str
    archive_url: str


@dataclass(frozen=True)
class SnapshotInfo:
    id: str
    version: str
    created_at: float
    reason: str


@dataclass(frozen=True)
class SnapshotMetadata:
    id: str
    version: str
    created_at: float
    reason: str
    wheel: str
    wheel_sha256: str
    database_present: bool
    config_present: bool
    config_sha256: str | None


class UpdateService:
    """Back up local state and install or restore Shea release code."""

    def __init__(
        self,
        data_dir: Path,
        *,
        db_path: Path | None = None,
        project_dir: Path | None = None,
        repository: str = "mwasgibson/Shea",
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.snapshots_dir = self.data_dir / "snapshots"
        self.db_path = (db_path or self.data_dir / "shea.db").expanduser().resolve()
        self.config_path = self.data_dir / "config.json"
        self.repository = repository
        self.releases_url = f"https://api.github.com/repos/{repository}/releases"
        self.project_dir = project_dir or self._find_project_dir()

    @staticmethod
    def _require_python_installation() -> None:
        if getattr(sys, "frozen", False):
            raise UpdateError(
                "In-place updates and code restore are not supported by standalone "
                "desktop packages. Download and run the matching installer from the "
                "Shea GitHub Releases page. App data is stored separately and is preserved."
            )

    def check_for_updates(
        self,
        current_version: str,
        channel: str = "stable",
    ) -> UpdateManifest | None:
        """Return the newest newer release in the requested channel, if one exists."""
        if channel not in {"stable", "prerelease"}:
            raise ValueError("channel must be 'stable' or 'prerelease'")

        releases = self._fetch_releases()
        for release in releases:
            if release.get("draft") is True:
                continue
            if channel == "stable" and release.get("prerelease") is True:
                continue

            tag = release.get("tag_name")
            if not isinstance(tag, str) or not self._is_newer_version(tag, current_version):
                continue

            archive_url = release.get("tarball_url")
            if not isinstance(archive_url, str):
                continue
            self._validate_archive_url(archive_url)

            changelog = release.get("body")
            if not isinstance(changelog, str) or not changelog.strip():
                changelog = str(release.get("name") or f"Shea {tag}")

            return UpdateManifest(
                version=self._version_from_tag(tag),
                channel=channel,
                requires_migration=False,
                changelog=changelog.strip(),
                tag_name=tag,
                archive_url=archive_url,
            )
        return None

    def create_snapshot(self, reason: str = "manual") -> Path:
        """Atomically save a rollback wheel, SQLite backup, and config snapshot."""
        self._require_python_installation()
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot_id = f"snapshot_{time.time_ns()}_{uuid.uuid4().hex[:8]}"
        temporary_dir = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=self.snapshots_dir))
        target_dir = self.snapshots_dir / snapshot_id

        try:
            wheel_dir = temporary_dir / "wheels"
            wheel_dir.mkdir()
            wheel = self._build_current_wheel(wheel_dir)
            shutil.move(str(wheel), temporary_dir / wheel.name)

            database_present = self.db_path.is_file()
            if database_present:
                self._backup_database(self.db_path, temporary_dir / "shea.db")

            config_present = self.config_path.is_file()
            if config_present:
                shutil.copy2(self.config_path, temporary_dir / "config.json")

            metadata: JsonObject = {
                "id": snapshot_id,
                "version": self._installed_version(),
                "created_at": time.time(),
                "reason": reason,
                "wheel": wheel.name,
                "wheel_sha256": self._sha256(temporary_dir / wheel.name),
                "database_present": database_present,
                "config_present": config_present,
                "config_sha256": (
                    self._sha256(temporary_dir / "config.json") if config_present else None
                ),
            }
            metadata_path = temporary_dir / "meta.json"
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
            os.replace(temporary_dir, target_dir)
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

        logger.info("Created update snapshot %s", snapshot_id)
        return target_dir

    def list_snapshots(self) -> list[SnapshotInfo]:
        if not self.snapshots_dir.is_dir():
            return []

        snapshots: list[SnapshotInfo] = []
        for path in self.snapshots_dir.iterdir():
            if path.is_symlink() or not path.is_dir() or not path.name.startswith("snapshot_"):
                continue
            try:
                metadata = self._read_metadata(path)
                snapshots.append(
                    SnapshotInfo(
                        id=metadata.id,
                        version=metadata.version,
                        created_at=metadata.created_at,
                        reason=metadata.reason,
                    )
                )
            except (OSError, KeyError, TypeError, json.JSONDecodeError, UpdateError):
                logger.warning("Ignoring invalid snapshot metadata in %s", path)
        return sorted(snapshots, key=lambda snapshot: snapshot.created_at, reverse=True)

    def restore(self, snapshot_id: str) -> None:
        """Restore application code and config, preserving the live DB and .env."""
        self._require_python_installation()
        target_dir = self._snapshot_path(snapshot_id)
        metadata = self._read_metadata(target_dir)
        wheel = self._verified_wheel(target_dir, metadata)

        rollback_dir = self.create_snapshot(reason=f"pre_restore_{snapshot_id}")
        try:
            self._install_wheel(wheel)
            self._restore_config(target_dir, metadata)
        except Exception as restore_error:
            try:
                rollback_metadata = self._read_metadata(rollback_dir)
                self._install_wheel(self._verified_wheel(rollback_dir, rollback_metadata))
                self._restore_config(rollback_dir, rollback_metadata)
            except Exception as rollback_error:
                raise UpdateError(
                    f"Restore failed ({restore_error}); automatic rollback also "
                    f"failed ({rollback_error})."
                ) from rollback_error
            raise UpdateError(
                f"Restore failed; the prior installation was restored: {restore_error}"
            ) from restore_error

    def rollback(self, snapshot_id: str) -> bool:
        """Compatibility wrapper for callers using the former rollback API."""
        self.restore(snapshot_id)
        return True

    def apply_update(self, update: UpdateManifest | str) -> bool:
        """Install a release after snapshotting the exact current package."""
        self._require_python_installation()
        manifest = update if isinstance(update, UpdateManifest) else self._release_for_tag(update)
        self._validate_archive_url(manifest.archive_url)
        snapshot_dir = self.create_snapshot(reason=f"pre_update_{manifest.version}")

        try:
            self._run_pip("install", "--upgrade", manifest.archive_url)
        except UpdateError as update_error:
            try:
                metadata = self._read_metadata(snapshot_dir)
                self._install_wheel(self._verified_wheel(snapshot_dir, metadata))
            except Exception as rollback_error:
                raise UpdateError(
                    f"Update failed ({update_error}); automatic rollback also "
                    f"failed ({rollback_error})."
                ) from rollback_error
            logger.error("Update failed; restored the previous package: %s", update_error)
            return False

        logger.info("Installed Shea %s from GitHub release %s", manifest.version, manifest.tag_name)
        return True

    def _fetch_releases(self) -> list[JsonObject]:
        request = urllib.request.Request(
            self.releases_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Shea-updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = cast(JsonValue, json.loads(response.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f"Could not check GitHub Releases: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise UpdateError("GitHub returned invalid release metadata.") from exc

        if not isinstance(payload, list):
            raise UpdateError("GitHub returned an unexpected release response.")
        return [release for release in payload if isinstance(release, dict)]

    def _release_for_tag(self, version: str) -> UpdateManifest:
        normalized_version = self._version_from_tag(version)
        expected_tags = {normalized_version, f"v{normalized_version}"}
        for release in self._fetch_releases():
            tag = release.get("tag_name")
            if not isinstance(tag, str) or tag not in expected_tags:
                continue
            archive_url = release.get("tarball_url")
            if not isinstance(archive_url, str):
                break
            self._validate_archive_url(archive_url)
            return UpdateManifest(
                version=self._version_from_tag(tag),
                channel="stable",
                requires_migration=False,
                changelog=str(release.get("body") or ""),
                tag_name=tag,
                archive_url=archive_url,
            )
        raise UpdateError(f"No published GitHub release exists for version {version!r}.")

    def _build_current_wheel(self, wheel_dir: Path) -> Path:
        source: str | Path | None = None
        if self.project_dir is not None:
            project_file = self.project_dir / "pyproject.toml"
            try:
                project = tomllib.loads(project_file.read_text())
                project_info = project.get("project", {})
                if (
                    project_info.get("name") == "shea"
                    and project_info.get("version") == self._installed_version()
                ):
                    source = self.project_dir
            except (OSError, tomllib.TOMLDecodeError, AttributeError):
                source = None

        if source is None:
            source = self._release_for_tag(self._installed_version()).archive_url

        self._run_pip("wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(source))
        wheels = list(wheel_dir.glob("shea-*.whl"))
        if len(wheels) != 1:
            raise UpdateError(
                "Could not create an unambiguous rollback wheel for the current version."
            )
        return wheels[0]

    def _run_pip(self, *arguments: str) -> None:
        command = [sys.executable, "-m", "pip", *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            raise UpdateError(f"Could not start the Python package installer: {exc}") from exc
        if result.returncode:
            details = result.stderr.strip().splitlines()
            detail = details[-1] if details else "pip returned a non-zero exit status."
            raise UpdateError(f"Package operation failed: {detail}")

    def _install_wheel(self, wheel: Path) -> None:
        self._run_pip("install", "--force-reinstall", str(wheel))

    def _backup_database(self, source: Path, destination: Path) -> None:
        source_conn = sqlite3.connect(str(source))
        destination_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(destination_conn)
        except sqlite3.Error as exc:
            raise UpdateError(f"Could not back up SQLite database {source}: {exc}") from exc
        finally:
            destination_conn.close()
            source_conn.close()

    def _restore_config(self, snapshot_dir: Path, metadata: SnapshotMetadata) -> None:
        if metadata.config_present:
            source = snapshot_dir / "config.json"
            if self._sha256(source) != metadata.config_sha256:
                raise UpdateError("Snapshot config checksum does not match its metadata.")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self.config_path.with_name(
                f".{self.config_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                shutil.copy2(source, temporary_path)
                os.replace(temporary_path, self.config_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        else:
            self.config_path.unlink(missing_ok=True)

    def _verified_wheel(self, snapshot_dir: Path, metadata: SnapshotMetadata) -> Path:
        if Path(metadata.wheel).name != metadata.wheel:
            raise UpdateError("Snapshot contains an invalid rollback wheel path.")
        wheel = snapshot_dir / metadata.wheel
        if not wheel.is_file() or self._sha256(wheel) != metadata.wheel_sha256:
            raise UpdateError("Snapshot rollback wheel is missing or corrupted.")
        return wheel

    def _read_metadata(self, snapshot_dir: Path) -> SnapshotMetadata:
        try:
            metadata = cast(JsonValue, json.loads((snapshot_dir / "meta.json").read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError(f"Could not read snapshot metadata in {snapshot_dir}.") from exc
        if not isinstance(metadata, dict) or metadata.get("id") != snapshot_dir.name:
            raise UpdateError(f"Snapshot metadata is invalid in {snapshot_dir}.")
        version = metadata.get("version")
        created_at = metadata.get("created_at")
        reason = metadata.get("reason")
        wheel = metadata.get("wheel")
        wheel_sha256 = metadata.get("wheel_sha256")
        database_present = metadata.get("database_present")
        config_present = metadata.get("config_present")
        config_sha256 = metadata.get("config_sha256")
        if (
            not isinstance(version, str)
            or not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
            or not isinstance(reason, str)
            or not isinstance(wheel, str)
            or not isinstance(wheel_sha256, str)
            or not isinstance(database_present, bool)
            or not isinstance(config_present, bool)
        ):
            raise UpdateError(f"Snapshot metadata has invalid field types in {snapshot_dir}.")
        if config_present and not isinstance(config_sha256, str):
            raise UpdateError(f"Snapshot config metadata is invalid in {snapshot_dir}.")
        if not config_present and config_sha256 is not None and not isinstance(config_sha256, str):
            raise UpdateError(f"Snapshot config metadata is invalid in {snapshot_dir}.")
        return SnapshotMetadata(
            id=snapshot_dir.name,
            version=version,
            created_at=float(created_at),
            reason=reason,
            wheel=wheel,
            wheel_sha256=wheel_sha256,
            database_present=database_present,
            config_present=config_present,
            config_sha256=config_sha256 if isinstance(config_sha256, str) else None,
        )

    def _snapshot_path(self, snapshot_id: str) -> Path:
        if not re.fullmatch(r"snapshot_[A-Za-z0-9_-]+", snapshot_id):
            raise UpdateError(f"Invalid snapshot ID {snapshot_id!r}.")
        path = self.snapshots_dir / snapshot_id
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != self.snapshots_dir:
            raise UpdateError(f"Snapshot {snapshot_id!r} does not exist.")
        return path

    def _installed_version(self) -> str:
        return __version__

    def _find_project_dir(self) -> Path | None:
        for parent in Path(__file__).resolve().parents:
            if (parent / "pyproject.toml").is_file():
                return parent
        return None

    @staticmethod
    def _version_from_tag(tag: str) -> str:
        return tag[1:] if tag.lower().startswith("v") else tag

    @classmethod
    def _is_newer_version(cls, candidate_tag: str, current_version: str) -> bool:
        candidate = cls._version_key(cls._version_from_tag(candidate_tag))
        current = cls._version_key(current_version)
        if candidate is None or current is None:
            return False
        candidate_core, candidate_release, candidate_prerelease = candidate
        current_core, current_release, current_prerelease = current
        width = max(len(candidate_core), len(current_core))
        candidate_core += (0,) * (width - len(candidate_core))
        current_core += (0,) * (width - len(current_core))
        if candidate_core != current_core:
            return candidate_core > current_core
        if candidate_release != current_release:
            return candidate_release
        if candidate_release:
            return False
        return candidate_prerelease > current_prerelease

    @staticmethod
    def _version_key(
        version: str,
    ) -> tuple[tuple[int, ...], bool, tuple[tuple[int, int | str], ...]] | None:
        match = re.fullmatch(
            r"(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?",
            version,
        )
        if match is None:
            return None
        core = tuple(int(part) for part in match.group(1).split("."))
        prerelease = match.group(2)
        identifiers: tuple[tuple[int, int | str], ...] = ()
        if prerelease is not None:
            identifiers = tuple(
                (0, int(part)) if part.isdigit() else (1, part.lower())
                for part in prerelease.split(".")
            )
        return core, prerelease is None, identifiers

    def _validate_archive_url(self, url: str) -> None:
        parsed = urlparse(url)
        expected_repo = f"/{self.repository}".lower()
        trusted_api_path = f"/repos/{self.repository}/tarball/".lower()
        trusted_web_path = f"{expected_repo}/archive/".lower()
        trusted_host = (
            parsed.hostname == "api.github.com" and parsed.path.lower().startswith(trusted_api_path)
        ) or (parsed.hostname == "github.com" and parsed.path.lower().startswith(trusted_web_path))
        if (
            parsed.scheme != "https"
            or parsed.hostname not in GITHUB_ARCHIVE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or not trusted_host
        ):
            raise UpdateError("Release archive URL must target the configured Shea GitHub repo.")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()