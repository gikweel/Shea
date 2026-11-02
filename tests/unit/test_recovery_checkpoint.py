from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shea.contracts.enums import TaskState
from shea.contracts.models import RecoveryCheckpoint, Task
from shea.persistence.sqlite.recovery_checkpoint_repository import (
    SqliteRecoveryCheckpointRepository,
)
from shea.recovery.checkpoint import RecoveryCheckpointService


def _task(task_id: str = "task-1") -> Task:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Task(
        id=task_id,
        session_id="session-1",
        request_id="request-1",
        state=TaskState.READY,
        created_at=now,
        updated_at=now,
        plan_id="plan-1",
    )


def test_checkpoint_is_persisted_and_loaded(
    conn,
    task_repository,
    unit_of_work,
) -> None:
    task_repository.save(_task())
    repository = SqliteRecoveryCheckpointRepository(conn, unit_of_work=unit_of_work)
    service = RecoveryCheckpointService(task_repository, repository)
    checkpoint = RecoveryCheckpoint(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-2",
        state="RUNNING",
        last_verified_step=1,
        metadata={"attempt": 2, "verified": True},
    )

    service.checkpoint(checkpoint)

    assert service.get("task-1") == checkpoint


def test_checkpoint_replaces_the_prior_resume_point(
    conn,
    task_repository,
    unit_of_work,
) -> None:
    task_repository.save(_task())
    repository = SqliteRecoveryCheckpointRepository(conn, unit_of_work=unit_of_work)
    service = RecoveryCheckpointService(task_repository, repository)
    service.checkpoint(
        RecoveryCheckpoint(
            task_id="task-1",
            plan_id="plan-1",
            step_id="step-1",
            state="RUNNING",
        )
    )
    latest = RecoveryCheckpoint(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-2",
        state="RECOVERING",
        last_verified_step=1,
    )

    service.checkpoint(latest)

    assert service.get("task-1") == latest


def test_checkpoint_rejects_unknown_tasks(
    conn,
    task_repository,
    unit_of_work,
) -> None:
    service = RecoveryCheckpointService(
        task_repository,
        SqliteRecoveryCheckpointRepository(conn, unit_of_work=unit_of_work),
    )

    with pytest.raises(ValueError, match="unknown task"):
        service.checkpoint(
            RecoveryCheckpoint(
                task_id="missing",
                plan_id=None,
                step_id=None,
                state="READY",
            )
        )


def test_checkpoint_rejects_a_different_plan(
    conn,
    task_repository,
    unit_of_work,
) -> None:
    task_repository.save(_task())
    service = RecoveryCheckpointService(
        task_repository,
        SqliteRecoveryCheckpointRepository(conn, unit_of_work=unit_of_work),
    )

    with pytest.raises(ValueError, match="does not match"):
        service.checkpoint(
            RecoveryCheckpoint(
                task_id="task-1",
                plan_id="other-plan",
                step_id="step-1",
                state="RUNNING",
            )
        )
