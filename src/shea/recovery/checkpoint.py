from __future__ import annotations

from shea.contracts.models import RecoveryCheckpoint
from shea.ports.repositories import RecoveryCheckpointRepository, TaskRepository


class RecoveryCheckpointService:
    """Maintains the durable point from which a task can safely resume."""

    def __init__(
        self,
        task_repository: TaskRepository,
        checkpoint_repository: RecoveryCheckpointRepository,
    ) -> None:
        self._tasks = task_repository
        self._checkpoints = checkpoint_repository

    def checkpoint(
        self,
        checkpoint: RecoveryCheckpoint,
    ) -> None:
        task = self._tasks.get(checkpoint.task_id)

        if task is None:
            raise ValueError(f"Cannot checkpoint unknown task {checkpoint.task_id!r}")

        if checkpoint.plan_id != task.plan_id:
            raise ValueError(
                f"Checkpoint plan {checkpoint.plan_id!r} does not match task plan {task.plan_id!r}."
            )
        if not checkpoint.state.strip():
            raise ValueError("Checkpoint state must not be empty.")
        if checkpoint.last_verified_step is not None and checkpoint.last_verified_step < 0:
            raise ValueError("last_verified_step must not be negative.")

        self._checkpoints.save(checkpoint)

    def get(self, task_id: str) -> RecoveryCheckpoint | None:
        return self._checkpoints.get_by_task(task_id)