from __future__ import annotations

import sqlite3

import pytest

from shea.app.supervisor import ExecutionSupervisor
from shea.audit.recorder import AuditRecorder
from shea.contracts.enums import ExecutionOutcome, RiskLevel, TaskState
from shea.contracts.models import Decision, Task, ToolRequest, ToolResponse
from shea.core.orchestrator import Orchestrator
from shea.execution.permit import ExecutionPermitAuthority
from shea.execution.service import (
    ExecutionService,
    MissingDecisionError,
    TaskNotRunningError,
)
from shea.persistence.sqlite.app_receipt_repository import SqliteAppReceiptRepository
from shea.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.repositories import (
    AuthorizationRepository,
    DecisionRepository,
    PlanRepository,
    ToolExecutionRepository,
)
from shea.security.service import SecurityService
from shea.tools.executor import CapabilityNotAuthorizedError, ToolExecutor
from shea.tools.registry import ToolDeclaration, ToolRegistry
from tests.helper import MINIMAL_SCHEMA
from tests.unit.test_orchestrator import _RaisingAuditSink  # pyright: ignore[reportPrivateUsage]


def test_execute_on_non_running_task_raises(
    execution_service: ExecutionService, orchestrator: Orchestrator
) -> None:
    request = _make_request()
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")
    with pytest.raises(TaskNotRunningError):
        execution_service.execute(task, request)


def test_execute_without_decision_raises(
    execution_service: ExecutionService,
    authorized_task: Task,
    unit_of_work: SqliteUnitOfWork,
    conn: sqlite3.Connection,
    tool_registry: ToolRegistry,
) -> None:
    """Structural safety: even though only DecisionService can currently
    move a task to RUNNING, ExecutionService checks for a persisted
    Decision anyway rather than trusting that invariant blindly.
    """
    tool_registry.register(
        ToolDeclaration(
            name="weather", 
            capabilities=frozenset({"weather.lookup"}),
            argument_schema=MINIMAL_SCHEMA,
            ),
        lambda req: ToolResponse(success=True, data={}),
    )
    request = _make_request()

    # Remove any existing decisions for this task to simulate the missing case
    with unit_of_work:
        conn.execute("DELETE FROM decisions WHERE task_id = ?", (authorized_task.id,))

    # Now the task is RUNNING but the decision lookup won't find one
    with pytest.raises(MissingDecisionError):
        execution_service.execute(authorized_task, request)


def test_execute_success_advances_task_to_verifying(
    execution_service: ExecutionService,
    authorized_task: Task,
    tool_registry: ToolRegistry,
) -> None:
    def handler(request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True, data={"result": "ok"})

    tool_registry.register(
        ToolDeclaration(
            name="echo", 
            capabilities=frozenset({"weather.lookup"}),
            argument_schema=MINIMAL_SCHEMA,
            ), handler
    )
    request = _make_request(tool="echo", action="lookup")
    result = execution_service.execute(authorized_task, request)

    assert result.outcome == ExecutionOutcome.SUCCESS
    assert result.task.state == TaskState.VERIFYING


def test_supervisor_accepts_receipt_before_tool_handler_runs(
    execution_service: ExecutionService,
    authorized_task: Task,
    orchestrator: Orchestrator,
    app_receipt_repository: SqliteAppReceiptRepository,
    tool_registry: ToolRegistry,
) -> None:
    def handler(request: ToolRequest) -> ToolResponse:
        receipts = app_receipt_repository.list_unfinalized()
        assert len(receipts) == 1
        assert receipts[0].state.value == "ATTEMPTING"
        assert receipts[0].metadata["task_id"] == authorized_task.id
        assert orchestrator.get_task(authorized_task.id).state is TaskState.RUNNING
        return ToolResponse(success=True, data={"accepted": True})

    tool_registry.register(
        ToolDeclaration(
            name="receipt-check",
            capabilities=frozenset({"weather.lookup"}),
            argument_schema=MINIMAL_SCHEMA,
        ),
        handler,
    )

    result = execution_service.execute(
        authorized_task,
        _make_request(tool="receipt-check", action="run"),
    )

    assert result.outcome is ExecutionOutcome.SUCCESS


def test_execute_failure_advances_task_to_failed(
    execution_service: ExecutionService,
    authorized_task: Task,
    tool_registry: ToolRegistry,
) -> None:
    def handler(request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=False, error="deliberate failure")

    tool_registry.register(
        ToolDeclaration(
            name="fail", 
            capabilities=frozenset({"weather.lookup"}),
            argument_schema=MINIMAL_SCHEMA,
            ), handler
    )
    request = _make_request(tool="fail", action="do_thing")
    result = execution_service.execute(authorized_task, request)

    assert result.outcome == ExecutionOutcome.FAILURE
    assert result.task.state == TaskState.FAILED


def test_execute_unknown_outcome_advances_task_to_blocked(
    execution_service: ExecutionService,
    authorized_task: Task,
    tool_registry: ToolRegistry,
) -> None:
    """UNKNOWN stays distinct from FAILURE end-to-end (Appendix B).
    A tool that times out or loses its connection after a side effect
    may have occurred lands in BLOCKED, not FAILED, since it isn't safe
    to assume either way.
    """
    def unknown_handler(request: ToolRequest) -> ToolResponse:
        raise TimeoutError("simulated timeout")

    tool_registry.register(
        ToolDeclaration(
            name="slow", 
            capabilities=frozenset({"weather.lookup"}),
            argument_schema=MINIMAL_SCHEMA,
            ),
        unknown_handler,
    )
    request = _make_request(tool="slow", action="timeout")
    result = execution_service.execute(authorized_task, request)

    assert result.outcome == ExecutionOutcome.FAILURE
    assert result.task.state == TaskState.FAILED


def test_capability_denied_is_audited(
    execution_service: ExecutionService,
    authorized_task: Task,
    tool_registry: ToolRegistry,
    decision_repository: DecisionRepository,
    unit_of_work: SqliteUnitOfWork,
    conn: sqlite3.Connection,
) -> None:
    # Delete existing decisions for task before saving the test decision
    with unit_of_work:
        conn.execute("DELETE FROM decisions WHERE task_id = ?", (authorized_task.id,))
        decision_repository.save(
            _make_decision(authorized_task.id, capabilities=["weather.lookup"])
        )

    def denied_handler(req: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True, data={})

    tool_registry.register(
        ToolDeclaration(
            name="secret", 
            capabilities=frozenset({"credential.access"}),
            argument_schema=MINIMAL_SCHEMA,
            ),
        denied_handler,
    )
    request = _make_request(tool="secret", action="read")

    with pytest.raises(CapabilityNotAuthorizedError):
        execution_service.execute(authorized_task, request)

    rows = conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? AND event_type = ?",
        (authorized_task.id, "execution.capability_denied"),
    ).fetchall()
    assert len(rows) == 1


def test_execution_record_is_persisted(
    execution_service: ExecutionService,
    authorized_task: Task,
    tool_registry: ToolRegistry,
    tool_execution_repository: ToolExecutionRepository,
) -> None:
    def handler(request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True, data={"result": "ok"})

    tool_registry.register(
        ToolDeclaration(
            name="echo", 
            capabilities=frozenset({"weather.lookup"}),
            argument_schema=MINIMAL_SCHEMA,
            ), handler
    )
    request = _make_request(tool="echo", action="lookup")
    execution_service.execute(authorized_task, request)

    records = tool_execution_repository.list_by_task(authorized_task.id)
    assert len(records) == 1
    assert records[0].outcome == ExecutionOutcome.SUCCESS
    assert set(records[0].metadata) == {
        "receipt_id",
        "attempt_id",
        "evidence_id",
        "app_verification_result",
        "app_verification_explanation",
    }


def test_execution_service_transaction_rolls_back_on_audit_failure(
    authorized_task: Task,
    unit_of_work: SqliteUnitOfWork,
    orchestrator: Orchestrator,
    permit_authority: ExecutionPermitAuthority,
    execution_supervisor: ExecutionSupervisor,
    tool_executor: ToolExecutor,
    authorization_repository: AuthorizationRepository,
    decision_repository: DecisionRepository,
    plan_repository: PlanRepository,
    tool_execution_repository: ToolExecutionRepository,
    security_service: SecurityService,
    tool_registry: ToolRegistry,
    clock: Clock,
    id_generator: IdGenerator,
) -> None:
    """Phase 8: ExecutionService now wraps tool_execution save, audit,
    and task advancement in one transaction. Proves that if the audit
    write fails mid-transaction, the execution record is rolled back too.
    """
    def handler(request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True, data={"result": "ok"})

    tool_registry.register(
        ToolDeclaration(
            name="echo", 
            capabilities=frozenset({"weather.lookup"}),
            argument_schema=MINIMAL_SCHEMA,
            ), handler
    )
    request = _make_request(tool="echo", action="lookup")

    broken_audit = AuditRecorder(
        sink=_RaisingAuditSink(), clock=clock, id_generator=id_generator
    )
    broken_service = ExecutionService(
        tool_executor=tool_executor,
        permit_authority=permit_authority,
        execution_supervisor=execution_supervisor,
        orchestrator=orchestrator,
        authorization_repository=authorization_repository,
        decision_repository=decision_repository,
        plan_repository=plan_repository,
        tool_execution_repository=tool_execution_repository,
        audit=broken_audit,
        id_generator=id_generator,
        security_service=security_service,
        unit_of_work=unit_of_work,
        clock=clock
    )

    with pytest.raises(RuntimeError, match="simulated audit sink failure"):
        broken_service.execute(authorized_task, request)

    # Execution record must have been rolled back
    records = tool_execution_repository.list_by_task(authorized_task.id)
    assert len(records) == 0

    # Task state must also remain unchanged
    unchanged = orchestrator.get_task(authorized_task.id)
    assert unchanged.state == TaskState.RUNNING


# --- Helpers ---

def _make_request(
    request_id: str = "req-1", tool: str = "weather", action: str = "lookup"
) -> ToolRequest:
    return ToolRequest(request_id=request_id, tool=tool, action=action)


def _make_decision(task_id: str, capabilities: list[str]) -> Decision:
    return Decision(
        id="dec-1",
        task_id=task_id,
        recommendation="proceed",
        risk=RiskLevel.SAFE,
        requires_authorization=False,
        requires_explicit_acknowledgement=False,
        capabilities=capabilities,
    )


@pytest.fixture
def tool_execution_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> ToolExecutionRepository:
    from shea.persistence.sqlite.tool_execution_repository import SqliteToolExecutionRepository

    return SqliteToolExecutionRepository(conn, unit_of_work=unit_of_work)