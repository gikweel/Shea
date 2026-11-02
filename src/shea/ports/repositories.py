from __future__ import annotations

from typing import Protocol

from shea.contracts.models import (
    AuditEvent,
    Authorization,
    Decision,
    Intent,
    Plan,
    RecoveryAttempt,
    RecoveryCheckpoint,
    RecoveryDecisionRecord,
    Request,
    RiskAssessment,
    Session,
    Task,
    ToolExecutionRecord,
    VerificationRecord,
)


class SessionRepository(Protocol):
    """Persists Session — the conversational context a Request happens
    in, independent of whether any Task is ever created within it."""

    def get_or_create(self, session_id: str, *, actor: str) -> Session: ...

    def touch(self, session_id: str) -> None: ...


class RequestRepository(Protocol):
    """Persists Request — technical doc Section 8.1. A Request is what
    the user said/asked; it exists whether or not it leads to a Task."""

    def save(self, request: Request) -> None: ...

    def record_response(self, request_id: str, response: str) -> None: ...

    def get(self, request_id: str) -> Request | None: ...

    def list_by_session(self, session_id: str) -> list[Request]: ...


class TaskRepository(Protocol):
    """Persists Task state — technical doc Section 14: plan/task state is
    persistent, not RAM-only. Any adapter (SQLite today, Postgres later)
    implements this without core/ knowing the difference (Constraint 9).
    """

    def save(self, task: Task) -> None: ...

    def get(self, task_id: str) -> Task | None: ...

    def list_by_session(self, session_id: str) -> list[Task]: ...

    def list_transient_tasks(self) -> list[Task]: ...


class PlanRepository(Protocol):
    """Persists Plan + PlanStep state."""

    def save(self, plan: Plan) -> None: ...

    def get_by_task(self, task_id: str) -> Plan | None: ...


class IntentRepository(Protocol):
    """Persists Intent records — technical doc Section 8.2. Intents are
    owned by a Request; `get_by_task` resolves through Task.request_id."""

    def save(self, intent: Intent) -> None: ...

    def get_by_request(self, request_id: str) -> Intent | None: ...

    def get_by_task(self, task_id: str) -> Intent | None: ...


class RiskAssessmentRepository(Protocol):
    """Persists RiskAssessment records — technical doc Section 12."""

    def save(self, risk_assessment: RiskAssessment) -> None: ...

    def get_by_task(self, task_id: str) -> RiskAssessment | None: ...


class DecisionRepository(Protocol):
    """Persists Decision records — technical doc Section 11."""

    def save(self, decision: Decision) -> None: ...

    def get_by_task(self, task_id: str) -> Decision | None: ...


class AuthorizationRepository(Protocol):
    """Persists Authorization records — technical doc Section 11.

    Authorizations are append-only in spirit: a task may accumulate more
    than one over its lifetime (e.g. re-authorization after recovery), so
    this is a list, not a single slot.
    """

    def save(self, authorization: Authorization) -> None: ...

    def get(self, authorization_id: str) -> Authorization | None: ...

    def list_by_task(self, task_id: str) -> list[Authorization]: ...


class AuditSink(Protocol):
    """Append-only audit event storage — technical doc Section 18.

    Implementations must not mutate or delete existing events; this port
    only exposes `record`, deliberately, to keep that invariant structural
    rather than a matter of discipline.
    """

    def record(self, event: AuditEvent) ->AuditEvent | None: ...


class ToolExecutionRepository(Protocol):
    """Persists ToolExecutionRecord — technical doc Section 7.2's "Tool
    Execution" entity. A task may accumulate more than one record over
    retries, so this is append-only and listed, like AuthorizationRepository.
    """

    def save(self, record: ToolExecutionRecord) -> None: ...

    def list_by_task(self, task_id: str) -> list[ToolExecutionRecord]: ...

    def get_latest_by_task(self, task_id: str) -> ToolExecutionRecord | None: ...

    def get_by_idempotency_key(self, idempotency_key: str) -> ToolExecutionRecord | None: ...


class VerificationRepository(Protocol):
    """Persists VerificationRecord — technical doc Section 7.2's
    "Verification" entity.
    """

    def save(self, record: VerificationRecord) -> None: ...

    def list_by_task(self, task_id: str) -> list[VerificationRecord]: ...

    def get_latest_by_task(self, task_id: str) -> VerificationRecord | None: ...


class RecoveryAttemptRepository(Protocol):
    """Persists RecoveryAttempt records, one per retry — used both to
    enforce a bounded number of attempts and to record each attempt's
    eventual compensation outcome.
    """

    def save(self, attempt: RecoveryAttempt) -> None: ...

    def list_by_task(self, task_id: str) -> list[RecoveryAttempt]: ...

    def get_latest_by_task(self, task_id: str) -> RecoveryAttempt | None: ...


class RecoveryCheckpointRepository(Protocol):
    """Persists the latest durable resume point for each task."""

    def save(self, checkpoint: RecoveryCheckpoint) -> None: ...

    def get_by_task(self, task_id: str) -> RecoveryCheckpoint | None: ...


class RecoveryDecisionRepository(Protocol):
    """Persists the recovery strategy selected for an execution."""

    def save(self, decision: RecoveryDecisionRecord) -> None: ...

    def get_latest_by_task(
        self,
        task_id: str,
    ) -> RecoveryDecisionRecord | None: ...

    def list_by_task(
        self,
        task_id: str,
    ) -> list[RecoveryDecisionRecord]: ...
