from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shea.app.adapters.application_linux import LinuxApplicationAdapter
from shea.app.adapters.application_macos import MacOSApplicationAdapter
from shea.app.adapters.application_stub import ApplicationStubAdapter
from shea.app.adapters.browser_local import LocalBrowserAdapter
from shea.app.adapters.browser_playwright import (
    PlaywrightBrowserAdapter,
    playwright_available,
)
from shea.app.adapters.network_local import LocalNetworkAdapter
from shea.app.adapters.tool_executor import ToolExecutorAdapter
from shea.app.identity import IdentityResolver, IdentityRevalidator
from shea.app.supervisor import ExecutionSupervisor
from shea.audit.recorder import AuditRecorder
from shea.config.resolver import ConfigResolver
from shea.contracts.models import Task, ToolRequest, ToolResponse
from shea.core.orchestrator import Orchestrator
from shea.decision.pending import PendingConfirmationStore
from shea.decision.policy import PolicyEngine
from shea.decision.risk import RiskEngine
from shea.decision.service import DecisionService
from shea.execution.authorization import RecordAuthorizationVerifier
from shea.execution.permit import ExecutionPermitAuthority
from shea.execution.service import ExecutionService
from shea.model.scripted import ScriptedModelProvider
from shea.persistence.sqlite.app_attempt_repository import SqliteAppAttemptRepository
from shea.persistence.sqlite.app_evidence_repository import SqliteAppEvidenceRepository
from shea.persistence.sqlite.app_idempotency_repository import SqliteAppIdempotencyRepository
from shea.persistence.sqlite.app_receipt_repository import SqliteAppReceiptRepository
from shea.persistence.sqlite.app_recovery_repository import SqliteAppRecoveryRepository
from shea.persistence.sqlite.app_verification_repository import SqliteAppVerificationRepository
from shea.persistence.sqlite.audit_sink import SqliteAuditSink
from shea.persistence.sqlite.authorization_repository import SqliteAuthorizationRepository
from shea.persistence.sqlite.connection import open_connection
from shea.persistence.sqlite.decision_repository import SqliteDecisionRepository
from shea.persistence.sqlite.intent_repository import SqliteIntentRepository
from shea.persistence.sqlite.migrator import run_migrations
from shea.persistence.sqlite.plan_repository import SqlitePlanRepository
from shea.persistence.sqlite.recovery_attempt_repository import SqliteRecoveryAttemptRepository
from shea.persistence.sqlite.request_repository import SqliteRequestRepository
from shea.persistence.sqlite.risk_repository import SqliteRiskAssessmentRepository
from shea.persistence.sqlite.session_repository import SqliteSessionRepository
from shea.persistence.sqlite.task_repository import SqliteTaskRepository
from shea.persistence.sqlite.tool_execution_repository import SqliteToolExecutionRepository
from shea.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from shea.persistence.sqlite.verification_repository import SqliteVerificationRepository
from shea.planning.service import PlanningService
from shea.planning.templates import PlanTemplateRegistry
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.repositories import PlanRepository
from shea.recovery.service import RecoveryService
from shea.security.filesystem_policy import FilesystemPolicy
from shea.security.gate import SecurityGate
from shea.security.injection import PromptInjectionDetector
from shea.security.network_policy import NetworkPolicy
from shea.security.service import SecurityService
from shea.tools.executor import ToolExecutor
from shea.tools.registry import ToolDeclaration, ToolRegistry
from shea.understanding.deterministic import DeterministicIntentMatcher
from shea.verification.service import VerificationService
from shea.verification.verifier import VerifierRegistry
from tests.helper import persist_accepted_receipt_for_test


class FrozenClock(Clock):
    """Advances by one second on every call, so successive events get
    strictly increasing timestamps without relying on wall-clock time —
    keeps ordering assertions deterministic.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._current = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        value = self._current
        self._current = self._current + timedelta(seconds=1)
        return value


class SequentialIdGenerator(IdGenerator):
    """Predictable ids (id-1, id-2, ...) so tests can assert on them
    directly instead of matching a UUID pattern.
    """

    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._counter = 0

    def new_id(self) -> str:
        self._counter += 1
        return f"{self._prefix}-{self._counter}"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "shea_test.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_connection(db_path)
    run_migrations(connection)
    yield connection
    connection.close()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def id_generator() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def unit_of_work(conn: sqlite3.Connection) -> SqliteUnitOfWork:
    # One instance shared by task_repository, audit_sink, orchestrator,
    # and security_service below — that sharing is what makes their
    # writes nest into a single transaction instead of three.
    return SqliteUnitOfWork(conn)


@pytest.fixture
def task_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteTaskRepository:
    return SqliteTaskRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def plan_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqlitePlanRepository:
    return SqlitePlanRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def audit_sink(conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork) -> SqliteAuditSink:
    return SqliteAuditSink(conn, unit_of_work=unit_of_work)


@pytest.fixture
def audit_recorder(
    audit_sink: SqliteAuditSink, clock: FrozenClock, id_generator: SequentialIdGenerator
) -> AuditRecorder:
    return AuditRecorder(sink=audit_sink, clock=clock, id_generator=id_generator)


@pytest.fixture
def orchestrator(
    task_repository: SqliteTaskRepository,
    audit_recorder: AuditRecorder,
    clock: FrozenClock,
    id_generator: SequentialIdGenerator,
    unit_of_work: SqliteUnitOfWork,
    app_receipt_repository: SqliteAppReceiptRepository,
) -> Orchestrator:
    return Orchestrator(
        task_repository=task_repository,
        audit=audit_recorder,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        receipt_acceptance_verifier=lambda task_id, receipt_id: (
            (receipt := app_receipt_repository.get(receipt_id)) is not None
            and receipt.state.value == "ATTEMPTING"
            and receipt.metadata.get("task_id") == task_id
        ),
    )


@pytest.fixture
def config_resolver() -> ConfigResolver:
    return ConfigResolver()


@pytest.fixture
def risk_assessment_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteRiskAssessmentRepository:
    return SqliteRiskAssessmentRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def decision_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteDecisionRepository:
    return SqliteDecisionRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def authorization_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteAuthorizationRepository:
    return SqliteAuthorizationRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine()


@pytest.fixture
def risk_engine() -> RiskEngine:
    return RiskEngine()


@pytest.fixture
def pending_confirmations() -> PendingConfirmationStore:
    return PendingConfirmationStore()

@pytest.fixture
def decision_service(
    policy_engine: PolicyEngine,
    risk_engine: RiskEngine,
    orchestrator: Orchestrator,
    decision_repository: SqliteDecisionRepository,
    risk_assessment_repository: SqliteRiskAssessmentRepository,
    authorization_repository: SqliteAuthorizationRepository,
    plan_repository: PlanRepository,
    audit_recorder: AuditRecorder,
    clock: FrozenClock,
    id_generator: SequentialIdGenerator,
    unit_of_work: SqliteUnitOfWork,
    pending_confirmations: PendingConfirmationStore,
) -> DecisionService:
    return DecisionService(
        policy_engine=policy_engine,
        risk_engine=risk_engine,
        orchestrator=orchestrator,
        decision_repository=decision_repository,
        risk_repository=risk_assessment_repository,
        authorization_repository=authorization_repository,
        plan_repository=plan_repository,
        audit=audit_recorder,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        pending_confirmations=pending_confirmations,
    )


@pytest.fixture
def ready_task(orchestrator: Orchestrator) -> Task:
    """A task already advanced through CREATED -> PLANNING -> READY, i.e.
    exactly the state DecisionService expects to receive one in (planning
    happens upstream of Decision in the pipeline; Phase 2 doesn't build
    the Planning subsystem, so tests drive the state machine directly).
    """
    task = orchestrator.create_task(session_id="session-1", request_id="req-1")
    orchestrator.advance(task.id, "start_planning")
    return orchestrator.advance(task.id, "plan_ready")


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    # Register some test tools with schemas
    registry.register_with_schema(
        name="filesystem.read",
        capabilities=frozenset({"filesystem.read"}),
        handler=lambda req: ToolResponse(success=True, data="file content"),
        description="Read a file",
        argument_schema={
            "path": {"type": "string", "required": True, "min_length": 1},
        },
    )
    registry.register_with_schema(
        name="network.fetch",
        capabilities=frozenset({"network.connect"}),
        handler=lambda req: ToolResponse(success=True, data="response"),
        description="Fetch a URL",
        argument_schema={
            "url": {"type": "string", "required": True, "pattern": "^https?://"},
            "timeout": {"type": "integer", "required": False, "default": 30, "min_value": 1},
        },
    )
    return registry


@pytest.fixture
def tool_executor(
    tool_registry: ToolRegistry,
    permit_authority: ExecutionPermitAuthority
) -> ToolExecutor:
    return ToolExecutor(
        tool_registry, 
        allow_unsafe_execution=True, 
        permit_authority=permit_authority
    )


@pytest.fixture
def tool_execution_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteToolExecutionRepository:
    return SqliteToolExecutionRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def execution_supervisor(
    tool_executor: ToolExecutor,
    conn: sqlite3.Connection,
    unit_of_work: SqliteUnitOfWork,
    clock: FrozenClock,
    id_generator: SequentialIdGenerator,
    network_policy: NetworkPolicy,
    authorization_repository: SqliteAuthorizationRepository,
    task_repository: SqliteTaskRepository,
    plan_repository: SqlitePlanRepository,
) -> ExecutionSupervisor:
    receipt_repository = SqliteAppReceiptRepository(
        conn,
        unit_of_work=unit_of_work,
    )
    attempt_repository = SqliteAppAttemptRepository(
        conn,
        unit_of_work=unit_of_work,
    )
    evidence_repository = SqliteAppEvidenceRepository(
        conn,
        unit_of_work=unit_of_work,
    )
    verification_repository = SqliteAppVerificationRepository(
        conn,
        unit_of_work=unit_of_work,
    )
    idempotency_repository = SqliteAppIdempotencyRepository(
        conn,
        unit_of_work=unit_of_work,
    )
    recovery_repository = SqliteAppRecoveryRepository(
        conn,
        unit_of_work=unit_of_work,
    )

    adapters: list[Any] = [
        LinuxApplicationAdapter(),
        MacOSApplicationAdapter(),
        ApplicationStubAdapter(),
        LocalNetworkAdapter(policy=network_policy),
        ToolExecutorAdapter(tool_executor),  # agent tools still last-or-first by supports()
    ]
    if playwright_available():
        adapters.append(PlaywrightBrowserAdapter())
    else:
        adapters.append(LocalBrowserAdapter())

    return ExecutionSupervisor(
        adapters=adapters,
        receipt_repository=receipt_repository,
        attempt_repository=attempt_repository,
        evidence_repository=evidence_repository,
        verification_repository=verification_repository,
        idempotency_repository=idempotency_repository,
        recovery_repository=recovery_repository,
        identity_resolver=IdentityResolver(),
        identity_revalidator=IdentityRevalidator(),
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=RecordAuthorizationVerifier(
            authorizations=authorization_repository,
            tasks=task_repository,
            plans=plan_repository,
            clock=clock,
        ),
    )

@pytest.fixture
def execution_service(
    permit_authority: ExecutionPermitAuthority,
    tool_executor: ToolExecutor,
    execution_supervisor: ExecutionSupervisor,
    orchestrator: Orchestrator,
    authorization_repository: SqliteAuthorizationRepository,
    decision_repository: SqliteDecisionRepository,
    plan_repository: SqlitePlanRepository,
    tool_execution_repository: SqliteToolExecutionRepository,
    audit_recorder: AuditRecorder,
    id_generator: SequentialIdGenerator,
    security_service: SecurityService,
    unit_of_work: SqliteUnitOfWork,
    clock: Clock,
) -> ExecutionService:
    return ExecutionService(
        permit_authority=permit_authority,
        tool_executor=tool_executor,
        execution_supervisor=execution_supervisor,
        orchestrator=orchestrator,
        decision_repository=decision_repository,
        authorization_repository=authorization_repository,
        plan_repository=plan_repository,
        tool_execution_repository=tool_execution_repository,
        audit=audit_recorder,
        id_generator=id_generator,
        security_service=security_service,
        unit_of_work=unit_of_work,
        clock=clock,
    )


@pytest.fixture
def authorized_task(decision_service: DecisionService, ready_task: Task) -> Task:
    """A READY task with a SAFE-risk decision and authorization.
    Uses `weather.lookup`,
    which matches no capability in either default policy list, keeping
    this fixture's authorized-capabilities set deliberately empty so
    tests can control exactly what gets authorized.
    """
    outcome = decision_service.evaluate_and_authorize(
        ready_task, capabilities=frozenset({"weather.lookup"})
    )
    return outcome.task


@pytest.fixture
def running_task(
    authorized_task: Task,
    orchestrator: Orchestrator,
    app_receipt_repository: SqliteAppReceiptRepository,
    clock: FrozenClock,
    id_generator: SequentialIdGenerator,
) -> Task:
    """A task transitioned after its test receipt was accepted."""
    receipt_id = persist_accepted_receipt_for_test(
        app_receipt_repository, authorized_task, clock, id_generator
    )
    return orchestrator.advance(
        authorized_task.id,
        "authorize_and_run",
        receipt_id=receipt_id,
    )


@pytest.fixture
def verification_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteVerificationRepository:
    return SqliteVerificationRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def verifier_registry() -> VerifierRegistry:
    return VerifierRegistry()


@pytest.fixture
def verification_service(
    verifier_registry: VerifierRegistry,
    orchestrator: Orchestrator,
    tool_execution_repository: SqliteToolExecutionRepository,
    verification_repository: SqliteVerificationRepository,
    audit_recorder: AuditRecorder,
    clock: FrozenClock,
    id_generator: SequentialIdGenerator,
    unit_of_work: SqliteUnitOfWork,
) -> VerificationService:
    return VerificationService(
        verifier_registry=verifier_registry,
        orchestrator=orchestrator,
        tool_execution_repository=tool_execution_repository,
        verification_repository=verification_repository,
        audit=audit_recorder,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
    )


@pytest.fixture
def recovery_attempt_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteRecoveryAttemptRepository:
    return SqliteRecoveryAttemptRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def recovery_service(
    orchestrator: Orchestrator,
    recovery_attempt_repository: SqliteRecoveryAttemptRepository,
    tool_execution_repository: SqliteToolExecutionRepository,
    verification_repository: SqliteVerificationRepository,
    decision_repository: SqliteDecisionRepository,
    authorization_repository: SqliteAuthorizationRepository,
    clock: Clock,
    audit_recorder: AuditRecorder,
    id_generator: SequentialIdGenerator,
    unit_of_work: SqliteUnitOfWork,
) -> RecoveryService:
    return RecoveryService(
        orchestrator=orchestrator,
        recovery_attempt_repository=recovery_attempt_repository,
        tool_execution_repository=tool_execution_repository,
        verification_repository=verification_repository,
        decision_repository=decision_repository,
        authorization_repository=authorization_repository,
        clock=clock,
        audit=audit_recorder,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
    )


@pytest.fixture
def verifying_task(
    execution_service: ExecutionService, authorized_task: Task, tool_registry: ToolRegistry
) -> Task:
    """A task carried through to VERIFYING via a real, successful
    execution — the state VerificationService expects to receive one in.
    """

    def handler(request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True, data={"result": "ok"})

    tool_registry.register(
        ToolDeclaration(name="echo", capabilities=frozenset({"weather.lookup"})), handler
    )
    request = ToolRequest(request_id="req-1", tool="echo", action="lookup")
    result = execution_service.execute(authorized_task, request)
    return result.task


@pytest.fixture
def failed_task(
    execution_service: ExecutionService, authorized_task: Task, tool_registry: ToolRegistry
) -> Task:
    """A task carried through to FAILED via a real, failing execution —
    the state RecoveryService.begin_recovery expects to receive one in.
    """

    def handler(request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=False, error="deliberate failure")

    tool_registry.register(
        ToolDeclaration(name="fail", capabilities=frozenset({"weather.lookup"})), handler
    )
    request = ToolRequest(request_id="req-1", tool="fail", action="do_thing")
    result = execution_service.execute(authorized_task, request)
    return result.task


@pytest.fixture
def intent_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteIntentRepository:
    return SqliteIntentRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def session_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork, clock: FrozenClock
) -> SqliteSessionRepository:
    return SqliteSessionRepository(conn, unit_of_work=unit_of_work, clock=clock)


@pytest.fixture
def request_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteRequestRepository:
    return SqliteRequestRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def deterministic_matcher() -> DeterministicIntentMatcher:
    return DeterministicIntentMatcher()


@pytest.fixture
def template_registry() -> PlanTemplateRegistry:
    return PlanTemplateRegistry()


@pytest.fixture
def scripted_model_provider() -> ScriptedModelProvider:
    return ScriptedModelProvider()


@pytest.fixture
def planning_service(
    orchestrator: Orchestrator,
    deterministic_matcher: DeterministicIntentMatcher,
    template_registry: PlanTemplateRegistry,
    tool_registry: ToolRegistry,
    intent_repository: SqliteIntentRepository,
    plan_repository: SqlitePlanRepository,
    session_repository: SqliteSessionRepository,
    request_repository: SqliteRequestRepository,
    audit_recorder: AuditRecorder,
    clock: FrozenClock,
    id_generator: SequentialIdGenerator,
    unit_of_work: SqliteUnitOfWork,
) -> PlanningService:
    return PlanningService(
        orchestrator=orchestrator,
        deterministic_matcher=deterministic_matcher,
        template_registry=template_registry,
        tool_registry=tool_registry,
        intent_repository=intent_repository,
        plan_repository=plan_repository,
        session_repository=session_repository,
        request_repository=request_repository,
        audit=audit_recorder,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
    )


@pytest.fixture
def network_policy() -> NetworkPolicy:
    return NetworkPolicy()


@pytest.fixture
def filesystem_policy() -> FilesystemPolicy:
    return FilesystemPolicy(allowed_roots=frozenset({"/home/shea/workspace"}))


@pytest.fixture
def security_gate(
    network_policy: NetworkPolicy, filesystem_policy: FilesystemPolicy
) -> SecurityGate:
    return SecurityGate(network_policy=network_policy, filesystem_policy=filesystem_policy)


@pytest.fixture
def injection_detector() -> PromptInjectionDetector:
    return PromptInjectionDetector()


@pytest.fixture
def security_service(
    security_gate: SecurityGate,
    injection_detector: PromptInjectionDetector,
    orchestrator: Orchestrator,
    audit_recorder: AuditRecorder,
    unit_of_work: SqliteUnitOfWork,
) -> SecurityService:
    return SecurityService(
        gate=security_gate,
        injection_detector=injection_detector,
        orchestrator=orchestrator,
        audit=audit_recorder,
        unit_of_work=unit_of_work,
    )

    
@pytest.fixture
def app_receipt_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteAppReceiptRepository:
    return SqliteAppReceiptRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def app_attempt_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteAppAttemptRepository:
    return SqliteAppAttemptRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def app_evidence_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteAppEvidenceRepository:
    return SqliteAppEvidenceRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def app_verification_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteAppVerificationRepository:
    return SqliteAppVerificationRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def app_idempotency_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteAppIdempotencyRepository:
    return SqliteAppIdempotencyRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def app_recovery_repository(
    conn: sqlite3.Connection, unit_of_work: SqliteUnitOfWork
) -> SqliteAppRecoveryRepository:
    return SqliteAppRecoveryRepository(conn, unit_of_work=unit_of_work)


@pytest.fixture
def permit_authority() -> ExecutionPermitAuthority:
    return ExecutionPermitAuthority()