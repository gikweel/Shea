from __future__ import annotations

import sqlite3

from shea.app.adapters.stub import StubAdapter
from shea.app.contracts import ExecutionContract
from shea.app.enums import AppOutcome, AttemptState, ReceiptState
from shea.app.supervisor import ExecutionSupervisor
from shea.persistence.sqlite.app_attempt_repository import SqliteAppAttemptRepository
from shea.persistence.sqlite.app_receipt_repository import SqliteAppReceiptRepository
from shea.persistence.sqlite.migrator import run_migrations
from shea.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from tests.helper import AllowTestAuthorizationVerifier


def test_supervisor_round_trip_sqlite(
    conn: sqlite3.Connection,
    unit_of_work: SqliteUnitOfWork,
    clock: Clock,
    id_generator: IdGenerator,
) -> None:
    run_migrations(conn)  # applies 0007 if not already applied by conftest

    receipts = SqliteAppReceiptRepository(conn, unit_of_work=unit_of_work)
    attempts = SqliteAppAttemptRepository(conn, unit_of_work=unit_of_work)
    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=receipts,
        attempt_repository=attempts,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )

    contract = ExecutionContract(
        contract_id="c-sql-1",
        authorization_id="auth-sql-1",
        capability="stub",
        operation="stub.echo",
        target="sqlite-round-trip",
    )
    result = supervisor.execute(contract)

    assert result.receipt.state is ReceiptState.FINALIZED
    assert result.attempt.state is AttemptState.FINALIZED
    assert result.adapter_result.outcome is AppOutcome.SUCCESS

    loaded = receipts.get(result.receipt.id)
    assert loaded is not None
    assert loaded.outcome is AppOutcome.SUCCESS
    assert loaded.contract_id == "c-sql-1"

    by_contract = receipts.get_by_contract("c-sql-1")
    assert by_contract is not None
    assert by_contract.id == result.receipt.id

    attempt_rows = attempts.list_by_receipt(result.receipt.id)
    assert len(attempt_rows) == 1
    assert attempt_rows[0].evidence.get("receipt_id") == result.receipt.id