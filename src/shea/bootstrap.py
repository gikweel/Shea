from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from shea.adapters.system import SystemClock, UuidIdGenerator
from shea.app.adapters.application_linux import LinuxApplicationAdapter
from shea.app.adapters.application_macos import MacOSApplicationAdapter
from shea.app.adapters.application_stub import ApplicationStubAdapter
from shea.app.adapters.application_windows import WindowsApplicationAdapter
from shea.app.adapters.base import Adapter
from shea.app.adapters.browser_local import LocalBrowserAdapter
from shea.app.adapters.browser_playwright import (
    PlaywrightBrowserAdapter,
    playwright_available,
)
from shea.app.adapters.network_local import LocalNetworkAdapter
from shea.app.adapters.process_local import LocalProcessAdapter
from shea.app.adapters.tool_executor import ToolExecutorAdapter
from shea.app.identity import IdentityResolver, IdentityRevalidator
from shea.app.interaction.service import InteractionService
from shea.app.recovery import ReconciliationResult
from shea.app.supervisor import ExecutionSupervisor
from shea.audit.recorder import AuditRecorder
from shea.bootstrap_demo import register_demo_intent
from shea.config.resolver import ConfigResolver
from shea.contracts.models import Task
from shea.core.orchestrator import Orchestrator
from shea.credentials.broker import SheaCredentialBroker
from shea.credentials.keyring_adapter import KeyringSecureStore
from shea.credentials.ports import CredentialBroker
from shea.credentials.service import CredentialService
from shea.decision.pending import PendingConfirmationStore
from shea.decision.policy import DEFAULT_AUTHORIZATION_REQUIRED_CAPABILITIES, PolicyEngine
from shea.decision.risk import RiskEngine
from shea.decision.service import DecisionService
from shea.events.bus import EventBus
from shea.events.channels import (
    CredentialChannel,
    DecisionChannel,
    InteractionChannel,
    SecurityChannel,
    TaskChannel,
)
from shea.execution.authorization import RecordAuthorizationVerifier
from shea.execution.permit import ExecutionPermitAuthority
from shea.execution.plan_runner import PlanRunner
from shea.execution.service import ExecutionService
from shea.extensions.loader import PluginLoader
from shea.memory.broker import MemoryBroker
from shea.memory.extractor import MemoryExtractor
from shea.memory.lifecycle.expiration import MemoryLifecycleEnforcer
from shea.memory.retrieval.ranking import BoundedContextAssembler, HybridMemoryRetriever
from shea.memory.service import MemoryService
from shea.persistence.sqlite.app_attempt_repository import SqliteAppAttemptRepository
from shea.persistence.sqlite.app_evidence_repository import SqliteAppEvidenceRepository
from shea.persistence.sqlite.app_idempotency_repository import SqliteAppIdempotencyRepository
from shea.persistence.sqlite.app_receipt_repository import SqliteAppReceiptRepository
from shea.persistence.sqlite.app_recovery_repository import SqliteAppRecoveryRepository
from shea.persistence.sqlite.app_verification_repository import SqliteAppVerificationRepository
from shea.persistence.sqlite.audit_sink import SqliteAuditSink
from shea.persistence.sqlite.authorization_repository import SqliteAuthorizationRepository
from shea.persistence.sqlite.chat_repository import SqliteChatRepository
from shea.persistence.sqlite.connection import open_connection
from shea.persistence.sqlite.decision_repository import SqliteDecisionRepository
from shea.persistence.sqlite.intent_repository import SqliteIntentRepository
from shea.persistence.sqlite.memory_repository import SqliteMemoryRepository
from shea.persistence.sqlite.migrator import run_migrations
from shea.persistence.sqlite.plan_repository import SqlitePlanRepository
from shea.persistence.sqlite.recovery_attempt_repository import SqliteRecoveryAttemptRepository
from shea.persistence.sqlite.request_repository import SqliteRequestRepository
from shea.persistence.sqlite.risk_repository import SqliteRiskAssessmentRepository
from shea.persistence.sqlite.session_repository import SqliteSessionRepository
from shea.persistence.sqlite.task_repository import SqliteTaskRepository
from shea.persistence.sqlite.tool_execution_repository import SqliteToolExecutionRepository
from shea.persistence.sqlite.unit_of_work import SqliteUnitOfWork
from shea.persistence.sqlite.vault_repository import SqliteVaultRepository
from shea.persistence.sqlite.verification_repository import SqliteVerificationRepository
from shea.persistence.vector.fts_adapter import FtsVectorStore
from shea.planning.service import PlanningService
from shea.planning.templates import PlanTemplateRegistry
from shea.ports.execution_boundary import ExecutionBoundary
from shea.ports.model_provider import ModelProvider
from shea.profiles.service import ProfileService
from shea.recovery.service import RecoveryService
from shea.security.filesystem_policy import FilesystemPolicy
from shea.security.gate import SecurityGate
from shea.security.injection import PromptInjectionDetector
from shea.security.network_policy import NetworkPolicy
from shea.security.sandbox import SandboxedExecutionBoundary
from shea.security.service import SecurityService
from shea.tools.builtin.register import register_builtin_tools
from shea.tools.executor import ToolExecutor
from shea.tools.registry import ToolRegistry
from shea.understanding.deterministic import DeterministicIntentMatcher
from shea.verification.service import VerificationService
from shea.verification.verifier import VerifierRegistry


@dataclass
class SheaRuntime:
    """Process-wide wired graph. Construct once at startup."""

    conn: sqlite3.Connection
    chat_repository: SqliteChatRepository
    session_repository: SqliteSessionRepository
    request_repository: SqliteRequestRepository
    intent_repository: SqliteIntentRepository
    plan_repository: SqlitePlanRepository
    interaction_service: InteractionService
    unit_of_work: SqliteUnitOfWork
    orchestrator: Orchestrator
    tool_registry: ToolRegistry
    execution_supervisor: ExecutionSupervisor
    execution_service: ExecutionService
    planning_service: PlanningService
    plan_runner: PlanRunner
    recovery_service: RecoveryService
    decision_service: DecisionService
    pending_confirmations: PendingConfirmationStore
    verification_service: VerificationService
    security_service: SecurityService
    event_bus: EventBus
    memory_service: MemoryService
    profile_service: ProfileService
    credential_broker: CredentialBroker
    credential_service: CredentialService
    memory_extractor: MemoryExtractor
    config_resolver: ConfigResolver

    def reconcile_app_plane(self) -> list[ReconciliationResult]:
        return self.recovery_service.reconcile_app_plane()

    def reconcile_stranded_tasks(self) -> list[Task]:
        return self.recovery_service.reconcile_stranded_tasks()


def _load_dotenv() -> None:
    """Load .env from the project root (parent of src/) or CWD, stdlib only.

    Vars already set in the environment are NOT overwritten — explicit exports
    always win. This means ``SHEA_MODEL_PROVIDER=ghost python -m shea`` still
    works as expected.
    """
    # Walk up from this file to find .env next to src/
    candidates = [
        Path(__file__).parent.parent.parent / ".env",  # repo root
        Path.cwd() / ".env",                           # wherever the process started
    ]
    for dotenv_path in candidates:
        if not dotenv_path.is_file():
            continue
        with open(dotenv_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")   # strip optional quotes
                if key and key not in os.environ:    # don't override existing vars
                    os.environ[key] = value
        break  # stop after the first .env found


def _filesystem_roots(value: Iterable[str] | None, config_resolver: ConfigResolver | None = None) -> frozenset[str]:
    if value is not None:
        return frozenset(value)
    if config_resolver:
        configured = config_resolver.resolve("SHEA_FILESYSTEM_ROOTS", None)
    else:
        configured = os.environ.get("SHEA_FILESYSTEM_ROOTS")
        
    if configured:
        return frozenset(item for item in configured.split(os.pathsep) if item)
    return frozenset({str(Path.cwd())})


def build_runtime(
    db_path: Path | str = ":memory:",
    *,
    workspace: Path | str = "./workspace",
    register_builtins: bool = True,
    filesystem_roots: Iterable[str] | None = None,
    execution_boundary: ExecutionBoundary | None = None,
    config_resolver: ConfigResolver | None = None,
    allow_unsafe_execution: bool = False,
    include_http_fetch: bool = False,
    model_provider: ModelProvider | None = None,
    register_demo_intents: bool = True,
    include_ep_network: bool = False,
    include_ep_application: bool = False,
    include_ep_process: bool = False,
) -> SheaRuntime:
    """Build the V1 runtime and reconcile unfinished app-plane work.

    ``allow_unsafe_execution`` is intended for local development only. A
    production caller should provide a real execution boundary instead of
    relying on the unsafe fallback in ``ToolExecutor``.
    """
    from shea.observability.tracer import configure_tracing
    configure_tracing()
    # Auto-load .env from the project root so credentials don't need manual export
    _load_dotenv()
    conn = open_connection(db_path)
    run_migrations(conn)
    unit_of_work = SqliteUnitOfWork(conn)
    chat_repository = SqliteChatRepository(conn, unit_of_work=unit_of_work)
    clock = SystemClock()
    id_generator = UuidIdGenerator()

    event_bus = EventBus()
    task_channel = TaskChannel(event_bus, clock, id_generator)

    task_repository = SqliteTaskRepository(conn, unit_of_work=unit_of_work)
    plan_repository = SqlitePlanRepository(conn, unit_of_work=unit_of_work)
    audit_sink = SqliteAuditSink(conn, unit_of_work=unit_of_work)
    audit = AuditRecorder(sink=audit_sink, clock=clock, id_generator=id_generator)
    orchestrator = Orchestrator(
        task_repository=task_repository,
        audit=audit,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        task_channel=task_channel,
    )
    if model_provider is not None:
        provider = model_provider
    else:
        from shea.provider.factory import build_provider_routing_service
        provider = build_provider_routing_service(audit=audit)

    decision_repository = SqliteDecisionRepository(conn, unit_of_work=unit_of_work)
    vault_repo = SqliteVaultRepository(conn, unit_of_work=unit_of_work)
    secure_store = KeyringSecureStore(service_name="shea_agent")
    credential_broker = SheaCredentialBroker(vault=vault_repo, secure_store=secure_store)
    credential_channel = CredentialChannel(event_bus, clock, id_generator)
    credential_service = CredentialService(
        vault=vault_repo,
        secure_store=secure_store,
        ids=id_generator,
        clock=clock,
        channel=credential_channel,
    )
    risk_repository = SqliteRiskAssessmentRepository(conn, unit_of_work=unit_of_work)
    authorization_repository = SqliteAuthorizationRepository(conn, unit_of_work=unit_of_work)
    intent_repository = SqliteIntentRepository(conn, unit_of_work=unit_of_work)
    session_repository = SqliteSessionRepository(conn, unit_of_work=unit_of_work, clock=clock)
    request_repository = SqliteRequestRepository(conn, unit_of_work=unit_of_work)
    tool_execution_repository = SqliteToolExecutionRepository(
        conn, unit_of_work=unit_of_work
    )
    verification_repository = SqliteVerificationRepository(conn, unit_of_work=unit_of_work)
    recovery_attempt_repository = SqliteRecoveryAttemptRepository(
        conn, unit_of_work=unit_of_work
    )

    registry = ToolRegistry()
    workspace = Path(workspace).resolve() if not isinstance(workspace, Path) else workspace
    workspace.mkdir(parents=True, exist_ok=True)
    matcher = DeterministicIntentMatcher()
    templates = PlanTemplateRegistry()
    network_policy = NetworkPolicy()
    configured_roots = filesystem_roots if filesystem_roots is not None else (str(workspace),)
    filesystem_policy = FilesystemPolicy(
        allowed_roots=_filesystem_roots(configured_roots)
    )
    verifier_registry = VerifierRegistry()
    if register_builtins and register_demo_intents:
        register_builtin_tools(
            registry,
            filesystem_policy=filesystem_policy,
            network_policy=network_policy,
            verifier_registry=verifier_registry,
            include_http_fetch=include_http_fetch,
        )
        register_demo_intent(matcher, templates, workspace=workspace)
    permit_authority = ExecutionPermitAuthority()
    execution_boundary = execution_boundary or SandboxedExecutionBoundary()
    tool_executor = ToolExecutor(
        registry,
        boundary=execution_boundary,
        allow_unsafe_execution=allow_unsafe_execution,
        permit_authority=permit_authority,
        require_permit=True,
    )
    adapters: list[Adapter] = []
    if include_ep_process:
        adapters.append(LocalProcessAdapter())
    if include_ep_application:
        adapters.extend(
            [
                LinuxApplicationAdapter(),
                MacOSApplicationAdapter(),
                WindowsApplicationAdapter(),
                ApplicationStubAdapter(),
            ]
        )
    if include_ep_network:
        adapters.append(
            LocalNetworkAdapter(
                policy=network_policy,
                credential_broker=credential_broker,
            )
        )
        adapters.append(LocalBrowserAdapter())
        if playwright_available():
            adapters.append(PlaywrightBrowserAdapter())
    adapters.append(
        ToolExecutorAdapter(
            tool_executor,
            permit_authority=permit_authority,
            credential_broker=credential_broker,
        )
    )

    receipt_repository = SqliteAppReceiptRepository(conn, unit_of_work=unit_of_work)
    attempt_repository = SqliteAppAttemptRepository(conn, unit_of_work=unit_of_work)
    evidence_repository = SqliteAppEvidenceRepository(conn, unit_of_work=unit_of_work)
    app_verification_repository = SqliteAppVerificationRepository(
        conn, unit_of_work=unit_of_work
    )
    idempotency_repository = SqliteAppIdempotencyRepository(
        conn, unit_of_work=unit_of_work
    )
    app_recovery_repository = SqliteAppRecoveryRepository(
        conn, unit_of_work=unit_of_work
    )
    supervisor = ExecutionSupervisor(
        adapters=adapters,
        receipt_repository=receipt_repository,
        attempt_repository=attempt_repository,
        evidence_repository=evidence_repository,
        verification_repository=app_verification_repository,
        idempotency_repository=idempotency_repository,
        recovery_repository=app_recovery_repository,
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

    decision_channel = DecisionChannel(event_bus, clock, id_generator)
    pending_confirmations = PendingConfirmationStore()
    # One policy/risk instance shared by the task path (DecisionService) and
    # the conversational path (InteractionService), so they can never drift.
    # `memory.write` needs authorization: Shea remembering things is a real
    # side effect even when no Task exists.
    policy_engine = PolicyEngine(
        authorization_required_capabilities=DEFAULT_AUTHORIZATION_REQUIRED_CAPABILITIES
        | {"memory.write"}
    )
    risk_engine = RiskEngine()
    decision_service = DecisionService(
        policy_engine=policy_engine,
        risk_engine=risk_engine,
        orchestrator=orchestrator,
        decision_repository=decision_repository,
        pending_confirmations=pending_confirmations,
        risk_repository=risk_repository,
        authorization_repository=authorization_repository,
        plan_repository=plan_repository,
        audit=audit,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        channel=decision_channel,
    )
    security_channel = SecurityChannel(event_bus, clock, id_generator)
    security_service = SecurityService(
        gate=SecurityGate(
            network_policy=network_policy,
            filesystem_policy=filesystem_policy,
        ),
        injection_detector=PromptInjectionDetector(),
        orchestrator=orchestrator,
        audit=audit,
        unit_of_work=unit_of_work,
        security_channel=security_channel,
    )
    from shea.events.channels import ExecutionChannel
    execution_channel = ExecutionChannel(event_bus, clock, id_generator)
    execution_service = ExecutionService(
        execution_channel=execution_channel,
        permit_authority=permit_authority,
        tool_executor=tool_executor,
        execution_supervisor=supervisor,
        orchestrator=orchestrator,
        decision_repository=decision_repository,
        authorization_repository=authorization_repository,
        plan_repository=plan_repository,
        tool_execution_repository=tool_execution_repository,
        audit=audit,
        id_generator=id_generator,
        security_service=security_service,
        unit_of_work=unit_of_work,
        clock=clock,
        credential_broker=credential_broker,
    )
    verification_service = VerificationService(
        verifier_registry=verifier_registry,
        orchestrator=orchestrator,
        tool_execution_repository=tool_execution_repository,
        verification_repository=verification_repository,
        audit=audit,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
    )
    memory_store = SqliteMemoryRepository(conn, unit_of_work=unit_of_work)
    vector_store = FtsVectorStore(conn, unit_of_work=unit_of_work)
    memory_retriever = HybridMemoryRetriever(vector_store, memory_store)
    context_assembler = BoundedContextAssembler(clock, retriever=memory_retriever)
    memory_lifecycle = MemoryLifecycleEnforcer(conn, clock, unit_of_work=unit_of_work)
    profile_service = ProfileService(conn, unit_of_work=unit_of_work)
    memory_service = MemoryService(
        memory_store=memory_store,
        vector_store=vector_store,
        retriever=memory_retriever,
        assembler=context_assembler,
        lifecycle=memory_lifecycle,
    )
    memory_broker = MemoryBroker(
        memory_service=memory_service,
        clock=clock,
        id_generator=id_generator,
        model_provider=provider,
    )
    
    planning_service = PlanningService(
        orchestrator=orchestrator,
        deterministic_matcher=matcher,
        template_registry=templates,
        tool_registry=registry,
        intent_repository=intent_repository,
        plan_repository=plan_repository,
        session_repository=session_repository,
        request_repository=request_repository,
        audit=audit,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        model_provider=provider,
        memory_service=memory_service,
        context_assembler=context_assembler,
    )
    recovery_service = RecoveryService(
        orchestrator=orchestrator,
        recovery_attempt_repository=recovery_attempt_repository,
        tool_execution_repository=tool_execution_repository,
        verification_repository=verification_repository,
        audit=audit,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        execution_supervisor=supervisor,
        decision_repository=decision_repository,
        authorization_repository=authorization_repository,
        clock=clock,
    )
    plan_runner = PlanRunner(
        decision_service=decision_service,
        execution_service=execution_service,
        verification_service=verification_service,
        security_service=security_service,
        plan_repository=plan_repository,
        tool_registry=registry,
        pending_confirmations=pending_confirmations,
        clock=clock,
    )
    
    memory_extractor = MemoryExtractor(
        event_bus=event_bus,
        memory_broker=memory_broker,
        intent_repository=intent_repository,
        request_repository=request_repository,
        tool_execution_repository=tool_execution_repository,
        model_provider=provider,
    )

    interaction_service = InteractionService(
        planning_service=planning_service,
        plan_runner=plan_runner,
        orchestrator=orchestrator,
        request_repository=request_repository,
        interaction_channel=InteractionChannel(event_bus, clock, id_generator),
        policy_engine=policy_engine,
        risk_engine=risk_engine,
        audit=audit,
    )

    runtime = SheaRuntime(
        conn=conn,
        chat_repository=chat_repository,
        session_repository=session_repository,
        request_repository=request_repository,
        intent_repository=intent_repository,
        plan_repository=plan_repository,
        unit_of_work=unit_of_work,
        orchestrator=orchestrator,
        tool_registry=registry,
        execution_supervisor=supervisor,
        execution_service=execution_service,
        planning_service=planning_service,
        interaction_service=interaction_service,
        plan_runner=plan_runner,
        recovery_service=recovery_service,
        decision_service=decision_service,
        pending_confirmations=pending_confirmations,
        verification_service=verification_service,
        security_service=security_service,
        event_bus=event_bus,
        memory_service=memory_service,
        profile_service=profile_service,
        credential_broker=credential_broker,
        credential_service=credential_service,
        memory_extractor=memory_extractor,
        config_resolver=cast('ConfigResolver', config_resolver),
    )
    runtime.reconcile_app_plane()
    runtime.reconcile_stranded_tasks()

    # Load dynamic extensions/plugins
    plugin_loader = PluginLoader(plugin_dir=f"{workspace}/plugins")
    plugin_loader.load_plugins(runtime)
    return runtime
if __name__ == "__main__":
    import os
    print("Initializing SHEA Runtime and migrating database...")
    db_path = os.environ.get("SHEA_DB_PATH", "shea.db")
    workspace = os.environ.get("SHEA_WORKSPACE", "./workspace")
    runtime = build_runtime(db_path=db_path, workspace=workspace)
    print(f"Success. Database created at {db_path}")