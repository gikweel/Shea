from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shea.app.enums import ReceiptState
from shea.audit.recorder import AuditRecorder
from shea.contracts.enums import TaskState
from shea.contracts.models import AuditEvent
from shea.core.orchestrator import (
    ExecutionReceiptNotAcceptedError,
    Orchestrator,
    TaskNotFoundError,
)
from shea.persistence.sqlite.app_receipt_repository import SqliteAppReceiptRepository
from shea.persistence.sqlite.task_repository import SqliteTaskRepository
from shea.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.state_machine.transitions import IllegalTransitionError
from tests.helper import persist_accepted_receipt_for_test


def test_create_task_starts_in_created_state(orchestrator: Orchestrator) -> None:
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")

    assert task.state == TaskState.CREATED
    assert task.session_id == "session-1"
    assert task.request_id == "req-1"


def test_create_task_is_persisted(orchestrator: Orchestrator) -> None:
    created = orchestrator.create_task(session_id="session-1", request_id="req-1")

    fetched = orchestrator.get_task(created.id)

    assert fetched.id == created.id
    assert fetched.state == TaskState.CREATED


def test_get_missing_task_raises(orchestrator: Orchestrator) -> None:
    with pytest.raises(TaskNotFoundError):
        orchestrator.get_task("does-not-exist")


def test_advance_happy_path_to_completed(
    orchestrator: Orchestrator,
    app_receipt_repository: SqliteAppReceiptRepository,
    clock: Clock,
    id_generator: IdGenerator,
) -> None:
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")

    task = orchestrator.advance(task.id, "start_planning")
    assert task.state == TaskState.PLANNING

    task = orchestrator.advance(task.id, "plan_ready")
    assert task.state == TaskState.READY

    receipt_id = persist_accepted_receipt_for_test(
        app_receipt_repository, task, clock, id_generator
    )
    task = orchestrator.advance(task.id, "authorize_and_run", receipt_id=receipt_id)
    assert task.state == TaskState.RUNNING

    task = orchestrator.advance(task.id, "execution_complete")
    assert task.state == TaskState.VERIFYING

    task = orchestrator.advance(task.id, "verified")
    assert task.state == TaskState.COMPLETED


def test_illegal_transition_raises_and_leaves_state_unchanged(
    orchestrator: Orchestrator,
) -> None:
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")

    with pytest.raises(IllegalTransitionError):
        orchestrator.advance(task.id, "authorize_and_run")  # can't skip planning

    unchanged = orchestrator.get_task(task.id)
    assert unchanged.state == TaskState.CREATED


def test_illegal_transition_is_still_audited(
    orchestrator: Orchestrator, conn: sqlite3.Connection
) -> None:
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")

    with pytest.raises(IllegalTransitionError):
        orchestrator.advance(task.id, "authorize_and_run")

    rows = conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? AND event_type = ?",
        (task.id, "task.transition.rejected"),
    ).fetchall()

    assert len(rows) == 1
    assert rows[0]["result"] == "illegal_transition"
    assert rows[0]["action"] == "authorize_and_run"


def test_running_transition_requires_a_supervisor_accepted_receipt(
    orchestrator: Orchestrator, ready_task
) -> None:
    with pytest.raises(ExecutionReceiptNotAcceptedError):
        orchestrator.advance(ready_task.id, "authorize_and_run", receipt_id="missing")

    assert orchestrator.get_task(ready_task.id).state is TaskState.READY


@pytest.mark.parametrize("invalid_reason", ["wrong_task", "finalized"])
def test_running_transition_rejects_receipts_not_accepted_for_this_task(
    invalid_reason: str,
    orchestrator: Orchestrator,
    ready_task,
    app_receipt_repository: SqliteAppReceiptRepository,
    clock: Clock,
    id_generator: IdGenerator,
) -> None:
    receipt_id = persist_accepted_receipt_for_test(
        app_receipt_repository, ready_task, clock, id_generator
    )
    receipt = app_receipt_repository.get(receipt_id)
    assert receipt is not None
    if invalid_reason == "wrong_task":
        receipt.metadata["task_id"] = "another-task"
    else:
        receipt.state = ReceiptState.FINALIZED
    app_receipt_repository.save(receipt)

    with pytest.raises(ExecutionReceiptNotAcceptedError):
        orchestrator.advance(ready_task.id, "authorize_and_run", receipt_id=receipt_id)

    assert orchestrator.get_task(ready_task.id).state is TaskState.READY


def test_successful_transition_is_audited(
    orchestrator: Orchestrator, conn: sqlite3.Connection
) -> None:
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")
    orchestrator.advance(task.id, "start_planning")

    rows = conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? AND event_type = ?",
        (task.id, "task.transition"),
    ).fetchall()

    assert len(rows) == 1
    assert rows[0]["result"] == "success"


def test_cancel_from_ready_state(orchestrator: Orchestrator) -> None:
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")
    orchestrator.advance(task.id, "start_planning")
    orchestrator.advance(task.id, "plan_ready")

    task = orchestrator.advance(task.id, "cancel")

    assert task.state == TaskState.CANCELLED

    with pytest.raises(IllegalTransitionError):
        orchestrator.advance(task.id, "authorize_and_run")


class _RaisingAuditSink:
    """Stands in for a healthy AuditSink but always fails — used to prove
    the task-state write and the audit write in Orchestrator.advance()
    now share one transaction (Phase 8 finding) rather than each
    committing independently.
    """

    def record(self, event: AuditEvent) -> None:
        raise RuntimeError("simulated audit sink failure")


def test_advance_rolls_back_task_state_when_audit_write_fails(
    conn: sqlite3.Connection,
    unit_of_work: SqliteUnitOfWork,
    task_repository: SqliteTaskRepository,
    clock: Clock,
    id_generator: IdGenerator,
    orchestrator: Orchestrator,
) -> None:
    """Before the Phase 8 fix, `task.save()` committed on its own inside
    `advance()` before the audit write ever ran — a failing audit sink
    would leave the state change durably persisted with no audit record
    for it. Proves that no longer happens: both writes share
    `unit_of_work`, so the failed audit write rolls the task update back
    with it, not just raises past an already-committed state change.
    """
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")

    broken_audit = AuditRecorder(sink=_RaisingAuditSink(), clock=clock, id_generator=id_generator)
    broken_orchestrator = Orchestrator(
        task_repository=task_repository,
        audit=broken_audit,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
    )

    with pytest.raises(RuntimeError, match="simulated audit sink failure"):
        broken_orchestrator.advance(task.id, "start_planning")

    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task.id,)).fetchone()
    assert row["state"] == TaskState.CREATED.value


def test_advance_success_path_still_commits_both_writes(
    db_path: Path, orchestrator: Orchestrator
) -> None:
    """The mirror property: on the ordinary success path, both writes
    are actually durable, not just pending in an open transaction that
    happens to still be readable through the same connection.
    """
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")
    orchestrator.advance(task.id, "start_planning")

    fresh_conn = sqlite3.connect(db_path)
    fresh_conn.row_factory = sqlite3.Row
    try:
        task_row = fresh_conn.execute(
            "SELECT state FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()
        audit_row = fresh_conn.execute(
            "SELECT * FROM audit_events WHERE task_id = ? AND event_type = 'task.transition'",
            (task.id,),
        ).fetchone()
    finally:
        fresh_conn.close()

    assert task_row["state"] == TaskState.PLANNING.value
    assert audit_row is not None