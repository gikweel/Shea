from __future__ import annotations

import json
import sqlite3
from typing import Any, cast

from shea.contracts.models import RecoveryCheckpoint

from .unit_of_work import SqliteUnitOfWork


class SqliteRecoveryCheckpointRepository:
    def __init__(self, conn: sqlite3.Connection, *, unit_of_work: SqliteUnitOfWork) -> None:
        self._conn = conn
        self._uow = unit_of_work

    def save(self, checkpoint: RecoveryCheckpoint) -> None:
        with self._uow:
            self._conn.execute(
                """
                INSERT INTO recovery_checkpoints (
                    task_id, plan_id, step_id, state, last_verified_step, metadata
                )
                VALUES (
                    :task_id, :plan_id, :step_id, :state, :last_verified_step, :metadata
                )
                ON CONFLICT(task_id) DO UPDATE SET
                    plan_id = excluded.plan_id,
                    step_id = excluded.step_id,
                    state = excluded.state,
                    last_verified_step = excluded.last_verified_step,
                    metadata = excluded.metadata,
                    updated_at = datetime('now')
                """,
                {
                    "task_id": checkpoint.task_id,
                    "plan_id": checkpoint.plan_id,
                    "step_id": checkpoint.step_id,
                    "state": checkpoint.state,
                    "last_verified_step": checkpoint.last_verified_step,
                    "metadata": json.dumps(checkpoint.metadata),
                },
            )

    def get_by_task(self, task_id: str) -> RecoveryCheckpoint | None:
        row = self._conn.execute(
            "SELECT * FROM recovery_checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return _row_to_checkpoint(row) if row is not None else None


def _row_to_checkpoint(row: sqlite3.Row) -> RecoveryCheckpoint:
    metadata: Any = json.loads(row["metadata"])
    if not isinstance(metadata, dict):
        raise ValueError(f"Recovery checkpoint metadata for task {row['task_id']!r} is invalid.")
    return RecoveryCheckpoint(
        task_id=row["task_id"],
        plan_id=row["plan_id"],
        step_id=row["step_id"],
        state=row["state"],
        last_verified_step=row["last_verified_step"],
        metadata=cast(dict[str, Any], metadata),
    )