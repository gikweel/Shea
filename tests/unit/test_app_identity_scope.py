from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shea.app.adapters.process_local import LocalProcessAdapter
from shea.app.adapters.stub import StubAdapter
from shea.app.contracts import ExecutionContract
from shea.app.enums import AppOutcome
from shea.app.exceptions import ContractValidationError
from shea.app.identities import (
    IdentityAssurance,
    IdentityKind,
    IdentityRequirements,
    RequestedTarget,
)
from shea.app.identity.resolver import IdentityResolver
from shea.app.identity.revalidator import IdentityRevalidator
from shea.app.memory import (
    InMemoryAttemptRepository,
    InMemoryReceiptRepository,
)
from shea.app.scope_enforce import evaluate_scope
from shea.app.scopes import (
    EnforcementStatus,
    ExecutionScope,
    IsolationPolicy,
    ProcessScope,
    ResourceLimits,
)
from shea.app.supervisor import ExecutionSupervisor
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier


def test_resolve_path_canonical(tmp_path: Path) -> None:
    target = RequestedTarget(kind=IdentityKind.PATH, value=str(tmp_path / "a" / "b.txt"))
    resolved = IdentityResolver().resolve(target, IdentityRequirements())
    assert resolved.kind is IdentityKind.PATH
    assert resolved.canonical_value.endswith("b.txt")


def test_strict_path_requires_parent(tmp_path: Path) -> None:
    missing_parent = tmp_path / "nope" / "file.txt"
    resolved = IdentityResolver().resolve(
        RequestedTarget(kind=IdentityKind.PATH, value=str(missing_parent)),
        IdentityRequirements(assurance=IdentityAssurance.STRICT),
    )
    with pytest.raises(ContractValidationError, match="parent"):
        IdentityRevalidator().verify(
            resolved,
            IdentityRequirements(assurance=IdentityAssurance.STRICT),
        )


def test_scope_fails_closed_on_required_cgroup() -> None:
    scope = ExecutionScope(
        scope_id="s1",
        isolation=IsolationPolicy(require_cgroup=True),
    )
    with pytest.raises(ContractValidationError, match="cgroup"):
        evaluate_scope(scope)


def test_scope_fails_closed_on_empty_filesystem_roots_with_filesystem_op() -> None:
    from shea.app.scopes import FilesystemScope
    scope = ExecutionScope(
        scope_id="s-fs",
        filesystem=FilesystemScope(allowed_roots=frozenset()),
    )
    contract = ExecutionContract(
        contract_id="c-fs",
        authorization_id="auth-1",
        capability="filesystem.read",
        operation="filesystem.read",
        target="/tmp/test",
    )
    with pytest.raises(ContractValidationError, match="filesystem.allowed_roots"):
        evaluate_scope(scope, contract)


def test_scope_allows_filesystem_with_non_empty_roots() -> None:
    import tempfile

    from shea.app.scopes import FilesystemScope
    with tempfile.TemporaryDirectory() as tmpdir:
        scope = ExecutionScope(
            scope_id="s-fs-ok",
            filesystem=FilesystemScope(allowed_roots=frozenset({tmpdir})),
        )
        contract = ExecutionContract(
            contract_id="c-fs-ok",
            authorization_id="auth-1",
            capability="filesystem.read",
            operation="filesystem.read",
            target=f"{tmpdir}/test",
        )
        report = evaluate_scope(scope, contract)
        assert report.filesystem["allowed_roots"] is EnforcementStatus.ENFORCED
        assert report.acceptable is True


def test_process_spawn_echo(
    clock: Clock, id_generator: IdGenerator, unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository, 
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    scope = ExecutionScope(
        scope_id="s-proc",
        process=ProcessScope(
            allowed_executables=frozenset({sys.executable, "python", "python3"}),
            inherit_environ=True,
        ),
        resources=ResourceLimits(wall_time_ms=10_000, output_bytes=64_000),
    )
    supervisor = ExecutionSupervisor(
        adapters=[LocalProcessAdapter(), StubAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    contract = ExecutionContract(
        contract_id="c-proc-1",
        authorization_id="auth-1",
        capability="process.execute",
        operation="process.run",
        target=sys.executable,
        arguments={"argv": [sys.executable, "-c", "print('shea-app')"]},
        requested_target=RequestedTarget(
            kind=IdentityKind.PATH, value=sys.executable
        ),
        identity_requirements=IdentityRequirements(assurance=IdentityAssurance.BASIC),
        scope=scope,
    )
    result = supervisor.execute(contract)
    assert result.adapter_result.outcome is AppOutcome.SUCCESS
    assert "shea-app" in result.adapter_result.evidence.get("stdout", "")

def test_resolve_url_canonical() -> None:
    target = RequestedTarget(kind=IdentityKind.URL, value="https://example.com/path?query=1")
    # IdentityRequirements defaults to allowing URL, assuming it allows all or URL is allowed
    # Wait, IdentityRequirements(allowed_kinds=frozenset({IdentityKind.URL}))
    resolved = IdentityResolver().resolve(target, IdentityRequirements(allowed_kinds=frozenset({IdentityKind.URL})))
    assert resolved.kind is IdentityKind.URL
    assert resolved.canonical_value == "https://example.com/path?query=1"


def test_strict_url_revalidator_accepts() -> None:
    target = RequestedTarget(kind=IdentityKind.URL, value="https://example.com")
    resolved = IdentityResolver().resolve(
        target,
        IdentityRequirements(allowed_kinds=frozenset({IdentityKind.URL}), assurance=IdentityAssurance.STRICT),
    )
    verified = IdentityRevalidator().verify(
        resolved,
        IdentityRequirements(assurance=IdentityAssurance.STRICT),
    )
    assert verified.verified is True
    assert verified.assurance is IdentityAssurance.STRICT


def test_resolve_application_canonical() -> None:
    target = RequestedTarget(kind=IdentityKind.APPLICATION, value="com.apple.Safari")
    resolved = IdentityResolver().resolve(target, IdentityRequirements(allowed_kinds=frozenset({IdentityKind.APPLICATION})))
    assert resolved.kind is IdentityKind.APPLICATION
    assert resolved.canonical_value == "com.apple.Safari"


def test_strict_application_revalidator_accepts() -> None:
    target = RequestedTarget(kind=IdentityKind.APPLICATION, value="com.apple.Safari")
    resolved = IdentityResolver().resolve(
        target,
        IdentityRequirements(allowed_kinds=frozenset({IdentityKind.APPLICATION}), assurance=IdentityAssurance.STRICT),
    )
    verified = IdentityRevalidator().verify(
        resolved,
        IdentityRequirements(assurance=IdentityAssurance.STRICT),
    )
    assert verified.verified is True
    assert verified.assurance is IdentityAssurance.STRICT
