from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shea.audit.recorder import AuditRecorder
from shea.contracts.enums import TaskState
from shea.contracts.models import Task
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.repositories import TaskRepository
from shea.ports.unit_of_work import UnitOfWork
from shea.state_machine.transitions import IllegalTransitionError, next_state

if TYPE_CHECKING:
    from shea.events.channels import TaskChannel


class TaskNotFoundError(Exception):
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"No task found with id {task_id!r}")


class Orchestrator:
    """Coordinates task lifecycle — technical doc Component: Core Orchestrator.

    Phase 1 scope, deliberately: this class knows how to create a task and
    move it between states via the state machine, and it audits every
    attempt (successful or rejected). It does NOT know how to plan,
    assess risk, call a model, or execute a tool — those subsystems will
    be given their own ports and will call into this orchestrator, not the
    other way around, keeping `MODEL != AUTHORITY` (Appendix B) true by
    construction: nothing outside this class can move a Task's state.

    Phase 8 finding: every method here writes a Task and records an audit
    event as two independently-committed repository calls. A crash
    between them left a state change durably persisted with no audit
    trail for it — the write pairs below are now wrapped in `unit_of_work`
    so they commit (or roll back) together. `unit_of_work` must be the
    same instance given to `task_repository` and `audit`'s sink, or the
    nesting that makes this atomic doesn't actually happen (see
    `shea.persistence.sqlite.unit_of_work.SqliteUnitOfWork`).
    """

    def __init__(
        self,
        task_repository: TaskRepository,
        audit: AuditRecorder,
        clock: Clock,
        id_generator: IdGenerator,
        *,
        unit_of_work: UnitOfWork,
        task_channel: TaskChannel | None = None,
    ) -> None:
        self._tasks = task_repository
        self._audit = audit
        self._clock = clock
        self._ids = id_generator
        self._uow = unit_of_work
        self._events = task_channel

    def create_task(
        self,
        *,
        session_id: str,
        request_id: str,
        profile_snapshot: dict[str, Any] | None = None,
    ) -> Task:
        now = self._clock.now()
        task = Task(
            id=self._ids.new_id(),
            session_id=session_id,
            request_id=request_id,
            state=TaskState.CREATED,
            created_at=now,
            updated_at=now,
            profile_snapshot=profile_snapshot,
        )
        with self._uow:
            self._tasks.save(task)
            self._audit.record(
                actor="orchestrator",
                component="core.orchestrator",
                event_type="task.created",
                action="create_task",
                result="success",
                request_id=request_id,
                task_id=task.id,
                metadata={"session_id": session_id},
            )
        if self._events is not None:
            self._events.created(task.id, session_id, request_id)
        return task

    def get_task(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def advance(
        self,
        task_id: str,
        event: str,
        receipt_id: str | None = None,
    ) -> Task:
        """Attempt to move `task_id` forward by `event`.

        Every call is audited, whether it succeeds or is rejected as
        illegal — a rejected transition is itself a security-relevant
        fact (something tried to move a task somewhere it wasn't allowed
        to go), not just an error to swallow.

        GAP 1 (Receipt-before-RUNNING, tightened):
        Any transition that lands in RUNNING requires a non-None
        receipt_id. If the task repository exposes get_receipt, the
        receipt must also exist and be in FINALIZED state. Callers that
        reach RUNNING (notably authorize_and_run) must supply the receipt.
        """
        task = self.get_task(task_id)

        try:
            new_state = next_state(task.state, event)
        except IllegalTransitionError as exc:
            self._audit.record(
                actor="orchestrator",
                component="core.orchestrator",
                event_type="task.transition.rejected",
                action=event,
                result="illegal_transition",
                request_id=task.request_id,
                task_id=task.id,
                metadata={"from_state": task.state.value},
            )
            raise exc

        # GAP 1 — Receipt-before-RUNNING gate (hard requirement)
        if new_state == TaskState.RUNNING:
            if not receipt_id:
                raise ValueError(
                    "Receipt must be provided and finalized before task can "
                    "transition to RUNNING (receipt_id is required)."
                )
            get_receipt = getattr(self._tasks, "get_receipt", None)
            if callable(get_receipt):
                receipt = get_receipt(receipt_id)
                # ReceiptState.FINALIZED lives in shea.app.enums; compare by value
                # so core does not hard-depend on the app plane at import time.
                receipt_state = getattr(receipt, "state", None)
                state_value = (
                    receipt_state.value
                    if hasattr(receipt_state, "value")
                    else str(receipt_state)
                    if receipt_state is not None
                    else None
                )
                if receipt is None or state_value != "FINALIZED":
                    raise ValueError(
                        f"Receipt {receipt_id} must be finalized before task can "
                        f"transition to RUNNING. Current receipt state: {state_value or 'none'}"
                    )

        from_state = task.state
        task.state = new_state
        task.updated_at = self._clock.now()

        with self._uow:
            self._tasks.save(task)
            self._audit.record(
                actor="orchestrator",
                component="core.orchestrator",
                event_type="task.transition",
                action=event,
                result="success",
                request_id=task.request_id,
                task_id=task.id,
                metadata={
                    "from_state": from_state.value,
                    "to_state": new_state.value,
                    "receipt_id": receipt_id,
                },
            )
        if self._events is not None:
            self._events.state_changed(
                task.id,
                from_state,
                new_state,
                event,
                request_id=task.request_id,
            )
        return task

    def attach_plan(self, task_id: str, plan_id: str) -> Task:
        """Records which Plan a task is executing, without touching its
        state. Kept on Orchestrator rather than letting PlanningService
        write directly to TaskRepository, so Task mutation stays
        centralized in one place — the same reason `advance()` is the
        only method that changes `state`.
        """
        task = self.get_task(task_id)
        task.plan_id = plan_id
        with self._uow:
            self._tasks.save(task)
            self._audit.record(
                actor="orchestrator",
                component="core.orchestrator",
                event_type="task.plan_attached",
                action="attach_plan",
                result="success",
                request_id=task.request_id,
                task_id=task.id,
                metadata={"plan_id": plan_id},
            )
        if self._events is not None:
            self._events.plan_attached(task.id, plan_id, request_id=task.request_id)
        return task

 def cancel_task(self, task_id: str, reason: str = "User cancelled") -> Task:
        task = self.get_task(task_id)
        if task.state in (
            TaskState.COMPLETED,
            TaskState.CANCELLED,
            TaskState.SECURITY_HALT,
            TaskState.FAILED,
            TaskState.BLOCKED,
        ):
            return task

        old_state = task.state
        task.state = TaskState.CANCELLED
        self._tasks.save(task)
        if self._events is not None:
            self._events.state_changed(
                task.id,
                old_state,
                task.state,
                "cancelled",
                request_id=task.request_id,
            )
        return task

    def list_transient_tasks(self) -> list[Task]:
        return self._tasks.list_transient_tasks()

    # -----------------------------------------------------------------------
    # GAP 4 — Phased deployment gate
    # Only allow advancing one phase at a time. Track current_phase in task
    # metadata or a separate config; call this before any phase-advancing work.
    # -----------------------------------------------------------------------
    def check_phase_gate(self, requested_phase: int, current_phase: int) -> bool:
        """Enforce phased deployment: only advance one phase at a time."""
        phase_rules: dict[int, set[int]] = {
            1: {0},  # Phase 1 can start from scratch
            2: {1},
            3: {2},
            4: {3},
            5: {4},
            6: {5},
            7: {6},
            8: {7},
            9: {8},  # Phase 9 requires Phase 8 complete
        }
        allowed_prereqs = phase_rules.get(requested_phase, set())
        return current_phase in allowed_prereqs

    def require_phase_gate(self, requested_phase: int, current_phase: int) -> None:
        """Raise if the requested phase is not allowed from current_phase."""
        if not self.check_phase_gate(requested_phase, current_phase):
            raise ValueError(
                f"Phase {requested_phase} cannot start until phase {current_phase} is complete"
            )
