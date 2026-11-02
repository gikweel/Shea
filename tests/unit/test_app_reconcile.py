from __future__ import annotations

import sqlite3
from typing import Any, cast

from shea.app.adapters.stub import StubAdapter
from shea.app.contracts import ExecutionAttempt, ExecutionContract, ExecutionReceipt
from shea.app.enums import (
    AppOutcome,
    AttemptState,
    ReceiptState,
    RecoveryStatus,
)
from shea.app.ports import AttemptRepository, ReceiptRepository
from shea.app.supervisor import ExecutionSupervisor
from shea.persistence.sqlite.app_recovery_repository import SqliteAppRecoveryRepository
from shea.persistence.sqlite.migrator import run_migrations
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier


def test_reconcile_invoked_not_finalized_becomes_unknown(
    conn: sqlite3.Connection,
    unit_of_work: UnitOfWork,
    clock: Clock,
    id_generator: IdGenerator,
    app_receipt_repository: ReceiptRepository,
    app_attempt_repository: AttemptRepository,
) -> None:
    run_migrations(conn)
    recovery_repo = SqliteAppRecoveryRepository(
        conn, unit_of_work=cast(Any, unit_of_work)
    )

    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        recovery_repository=recovery_repo,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )

    now = clock.now()
    receipt = ExecutionReceipt(
        id=id_generator.new_id(),
        contract_id="c-stuck",
        authorization_id="auth-1",
        capability="stub",
        operation="stub.echo",
        target="stuck",
        state=ReceiptState.ATTEMPTING,
        created_at=now,
        adapter_name="stub",
    )
    attempt = ExecutionAttempt(
        id=id_generator.new_id(),
        receipt_id=receipt.id,
        attempt_number=1,
        state=AttemptState.INVOKED,
        created_at=now,
        invoked_at=now,
        adapter_name="stub",
    )
    with unit_of_work:
        app_receipt_repository.save(receipt)
        app_attempt_repository.save(attempt)

    result = supervisor.reconcile_receipt(receipt.id)

    assert result.outcome is AppOutcome.UNKNOWN
    assert result.detail == "default: attempt_invoked_not_finalized; external state unknowable"
    assert result.receipt.state is ReceiptState.FINALIZED
    assert result.attempt is not None
    assert result.attempt.state is AttemptState.FINALIZED
    assert result.incident is not None
    assert result.incident.status is RecoveryStatus.QUARANTINED
    assert result.incident.fence_token is not None

    open_incidents = recovery_repo.list_open()
    assert any(i.id == result.incident.id for i in open_incidents)


def test_reconcile_already_finalized_is_noop(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: ReceiptRepository,
    app_attempt_repository: AttemptRepository,
) -> None:
    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    # Happy path finalize
    done = supervisor.execute(
        ExecutionContract(
            contract_id="c-done",
            authorization_id="auth-1",
            capability="stub",
            operation="stub.echo",
            target="done",
        )
    )
    again = supervisor.reconcile_receipt(done.receipt.id)
    assert again.detail == "nothing_to_reconcile"
    assert again.incident is None
    assert again.outcome is AppOutcome.SUCCESS