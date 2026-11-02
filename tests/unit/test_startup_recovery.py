from __future__ import annotations

from shea.contracts.enums import TaskState
from shea.core.orchestrator import Orchestrator
from shea.persistence.sqlite.app_receipt_repository import SqliteAppReceiptRepository
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.recovery.startup import StartupRecoveryService
from tests.helper import persist_accepted_receipt_for_test


def test_startup_reconciles_interrupted_receipt_into_failed_task(
    ready_task,
    task_repository,
    orchestrator: Orchestrator,
    execution_supervisor,
    conn,
    unit_of_work,
    clock: Clock,
    id_generator: IdGenerator,
) -> None:
    receipt_repository = SqliteAppReceiptRepository(conn, unit_of_work=unit_of_work)
    receipt_id = persist_accepted_receipt_for_test(
        receipt_repository, ready_task, clock, id_generator
    )
    task = orchestrator.advance(
        ready_task.id, "authorize_and_run", receipt_id=receipt_id
    )

    service = StartupRecoveryService(
        task_repository=task_repository,
        orchestrator=orchestrator,
        app_receipt_repository=receipt_repository,
        app_supervisor=execution_supervisor,
    )

    assert service.reconcile_app_receipts() == [task.id]
    assert orchestrator.get_task(task.id).state is TaskState.FAILED
