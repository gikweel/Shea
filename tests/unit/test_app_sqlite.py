from __future__ import annotations

import sqlite3
from typing import Any, cast

from shea.app.adapters.stub import StubAdapter
from shea.app.contracts import ExecutionContract
from shea.app.enums import AppOutcome, IdempotencyState
from shea.app.ports import AttemptRepository, ReceiptRepository
from shea.app.supervisor import ExecutionSupervisor
from shea.persistence.sqlite.app_evidence_repository import SqliteAppEvidenceRepository
from shea.persistence.sqlite.app_idempotency_repository import (
    SqliteAppIdempotencyRepository,
)
from shea.persistence.sqlite.app_verification_repository import (
    SqliteAppVerificationRepository,
)
from shea.persistence.sqlite.migrator import run_migrations
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier


def test_phase4_persists_evidence_and_verification(
    conn: sqlite3.Connection,
    unit_of_work: UnitOfWork,
    clock: Clock,
    id_generator: IdGenerator,
    app_receipt_repository: ReceiptRepository,
    app_attempt_repository: AttemptRepository,
) -> None:
    run_migrations(conn)
    evidence_repo = SqliteAppEvidenceRepository(
        conn, unit_of_work=cast(Any, unit_of_work)
    )
    verification_repo = SqliteAppVerificationRepository(
        conn, unit_of_work=cast(Any, unit_of_work)
    )

    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
        evidence_repository=evidence_repo,
        verification_repository=verification_repo,
    )
    result = supervisor.execute(
        ExecutionContract(
            contract_id="c-p4-sql",
            authorization_id="auth-1",
            capability="stub",
            operation="stub.echo",
            target="sqlite",
        )
    )

    rows = evidence_repo.list_by_receipt(result.receipt.id)
    assert len(rows) == 1
    assert rows[0].id == result.evidence_id

    ver = verification_repo.get_latest_by_receipt(result.receipt.id)
    assert ver is not None
    assert ver.result is AppOutcome.SUCCESS
    assert ver.id == result.verification.id  # type: ignore[union-attr]


def test_idempotency_blocks_second_execute(
    conn: sqlite3.Connection,
    unit_of_work: UnitOfWork,
    clock: Clock,
    id_generator: IdGenerator,
    app_receipt_repository: ReceiptRepository,
    app_attempt_repository: AttemptRepository,
) -> None:
    import pytest

    from shea.app.exceptions import ContractValidationError

    run_migrations(conn)
    idem_repo = SqliteAppIdempotencyRepository(
        conn, unit_of_work=cast(Any, unit_of_work)
    )

    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
        idempotency_repository=idem_repo,
    )
    contract = ExecutionContract(
        contract_id="c-idem-1",
        authorization_id="auth-1",
        capability="stub",
        operation="stub.echo",
        target="once",
        metadata={"idempotency_key": "key-abc"},
    )
    first = supervisor.execute(contract)
    assert first.receipt.outcome is AppOutcome.SUCCESS

    stored = idem_repo.get("key-abc")
    assert stored is not None
    assert stored.state is IdempotencyState.COMPLETED

    with pytest.raises(ContractValidationError, match="completed"):
        supervisor.execute(
            ExecutionContract(
                contract_id="c-idem-2",
                authorization_id="auth-1",
                capability="stub",
                operation="stub.echo",
                target="once",
                metadata={"idempotency_key": "key-abc"},
            )
        )