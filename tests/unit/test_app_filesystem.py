from __future__ import annotations

from pathlib import Path

import pytest

from shea.app.adapters.filesystem_local import LocalFilesystemAdapter
from shea.app.contracts import ExecutionContract
from shea.app.enums import AppOutcome, ReceiptState
from shea.app.identities import (
    IdentityAssurance,
    IdentityKind,
    IdentityRequirements,
    RequestedTarget,
)
from shea.app.memory import (
    InMemoryAttemptRepository,
    InMemoryReceiptRepository,
)
from shea.app.scopes import ExecutionScope, FilesystemScope
from shea.app.supervisor import ExecutionSupervisor
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier


def _scope(root: Path) -> ExecutionScope:
    return ExecutionScope(
        scope_id="fs-test",
        filesystem=FilesystemScope(allowed_roots=frozenset({str(root)})),
    )


def _supervisor(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    receipt_repository: InMemoryReceiptRepository,
    attempt_repository: InMemoryAttemptRepository,
) -> ExecutionSupervisor:
    return ExecutionSupervisor(
        adapters=[LocalFilesystemAdapter()],
        receipt_repository=receipt_repository,
        attempt_repository=attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )


def test_write_read_round_trip(
    tmp_path: Path,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "note.txt"
    supervisor = _supervisor(
        clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )

    write_contract = ExecutionContract(
        contract_id="c-fs-w",
        authorization_id="auth-1",
        capability="filesystem.write",
        operation="filesystem.write",
        target=str(target),
        arguments={"path": str(target), "content": "phase3"},
        requested_target=RequestedTarget(kind=IdentityKind.PATH, value=str(target)),
        identity_requirements=IdentityRequirements(assurance=IdentityAssurance.BASIC),
        scope=_scope(root),
    )
    w = supervisor.execute(write_contract)
    assert w.adapter_result.outcome is AppOutcome.SUCCESS
    assert w.receipt.state is ReceiptState.FINALIZED
    assert target.read_text(encoding="utf-8") == "phase3"

    read_contract = ExecutionContract(
        contract_id="c-fs-r",
        authorization_id="auth-2",
        capability="filesystem.read",
        operation="filesystem.read",
        target=str(target),
        arguments={"path": str(target)},
        requested_target=RequestedTarget(kind=IdentityKind.PATH, value=str(target)),
        identity_requirements=IdentityRequirements(assurance=IdentityAssurance.BASIC),
        scope=_scope(root),
    )
    r = supervisor.execute(read_contract)
    assert r.adapter_result.outcome is AppOutcome.SUCCESS
    assert r.adapter_result.evidence.get("content") == "phase3"


def test_path_outside_roots_denied(
    tmp_path: Path,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    supervisor = _supervisor(
        clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )
    contract = ExecutionContract(
        contract_id="c-fs-deny",
        authorization_id="auth-1",
        capability="filesystem.read",
        operation="filesystem.read",
        target=str(outside),
        arguments={"path": str(outside)},
        requested_target=RequestedTarget(kind=IdentityKind.PATH, value=str(outside)),
        identity_requirements=IdentityRequirements(assurance=IdentityAssurance.BASIC),
        scope=_scope(root),
    )
    result = supervisor.execute(contract)
    assert result.adapter_result.outcome is AppOutcome.FAILURE
    assert "blocked" in (result.adapter_result.error or "").lower() or "escape" in (
        result.adapter_result.error or ""
    ).lower() or "Path" in (result.adapter_result.error or "")


def test_symlink_escape_denied(
    tmp_path: Path,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available")

    supervisor = _supervisor(
        clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )
    contract = ExecutionContract(
        contract_id="c-fs-symlink",
        authorization_id="auth-1",
        capability="filesystem.read",
        operation="filesystem.read",
        target=str(link),
        arguments={"path": str(link)},
        requested_target=RequestedTarget(kind=IdentityKind.PATH, value=str(link)),
        identity_requirements=IdentityRequirements(assurance=IdentityAssurance.BASIC),
        scope=_scope(root),
    )
    result = supervisor.execute(contract)
    assert result.adapter_result.outcome is AppOutcome.FAILURE


def test_read_only_scope_blocks_write(
    tmp_path: Path,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "x.txt"
    scope = ExecutionScope(
        scope_id="ro",
        filesystem=FilesystemScope(
            allowed_roots=frozenset({str(root)}), read_only=True
        ),
    )
    supervisor = _supervisor(
        clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )
    result = supervisor.execute(
        ExecutionContract(
            contract_id="c-ro",
            authorization_id="auth-1",
            capability="filesystem.write",
            operation="filesystem.write",
            target=str(target),
            arguments={"path": str(target), "content": "x"},
            scope=scope,
        )
    )
    assert result.adapter_result.outcome is AppOutcome.FAILURE
    assert "read_only" in (result.adapter_result.error or "")


def test_copy_and_move_round_trip(
    tmp_path: Path,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    source = root / "source.txt"
    copied = root / "copied.txt"
    moved = root / "moved.txt"
    source.write_text("payload", encoding="utf-8")
    supervisor = _supervisor(
        clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )

    def execute(operation: str, path: Path, destination: Path, contract_id: str):
        return supervisor.execute(
            ExecutionContract(
                contract_id=contract_id,
                authorization_id=f"auth-{contract_id}",
                capability="filesystem.write",
                operation=operation,
                target=str(path),
                arguments={"path": str(path), "destination": str(destination)},
                scope=_scope(root),
            )
        )

    copied_result = execute("filesystem.copy", source, copied, "c-fs-copy")
    assert copied_result.adapter_result.outcome is AppOutcome.SUCCESS
    assert source.read_text(encoding="utf-8") == "payload"
    assert copied.read_text(encoding="utf-8") == "payload"

    moved_result = execute("filesystem.move", copied, moved, "c-fs-move")
    assert moved_result.adapter_result.outcome is AppOutcome.SUCCESS
    assert not copied.exists()
    assert moved.read_text(encoding="utf-8") == "payload"


def test_copy_denies_destination_outside_scope(
    tmp_path: Path,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    source = root / "source.txt"
    source.write_text("payload", encoding="utf-8")
    destination = tmp_path / "outside.txt"
    supervisor = _supervisor(
        clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )

    result = supervisor.execute(
        ExecutionContract(
            contract_id="c-fs-copy-deny",
            authorization_id="auth-copy-deny",
            capability="filesystem.write",
            operation="filesystem.copy",
            target=str(source),
            arguments={"path": str(source), "destination": str(destination)},
            scope=_scope(root),
        )
    )
    assert result.adapter_result.outcome is AppOutcome.FAILURE
    assert not destination.exists()