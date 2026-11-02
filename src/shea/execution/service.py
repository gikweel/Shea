from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from shea.app.adapters.tool_executor import ToolExecutorAdapter
from shea.app.contracts import ExecutionContract, ExecutionReceipt
from shea.app.identities import (
    IdentityAssurance,
)
from shea.app.identities import (
    IdentityKind as IdentityKind,
)
from shea.app.identities import (
    IdentityRequirements as IdentityRequirements,
)
from shea.app.identities import (
    RequestedTarget as RequestedTarget,
)
from shea.app.supervisor import ExecutionSupervisor
from shea.audit.recorder import AuditRecorder
from shea.contracts.enums import ExecutionOutcome, TaskState
from shea.contracts.models import Task, ToolExecutionRecord, ToolRequest, ToolResponse
from shea.core.orchestrator import Orchestrator
from shea.credentials.ports import CredentialBroker
from shea.events.channels import ExecutionChannel
from shea.execution.authorization import check_authorization_binding
from shea.execution.permit import ExecutionPermitAuthority
from shea.observability.metrics import global_metrics
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.repositories import (
    AuthorizationRepository,
    DecisionRepository,
    IntentRepository,
    PlanRepository,
    ToolExecutionRepository,
)
from shea.ports.unit_of_work import UnitOfWork
from shea.recovery.idempotency import IdempotencyKeyGenerator
from shea.security.service import SecurityService

if TYPE_CHECKING:
    from shea.tools.executor import ToolExecutor

_ADVANCE_EVENT_BY_OUTCOME: dict[ExecutionOutcome, str] = {
    ExecutionOutcome.SUCCESS: "execution_complete",
    ExecutionOutcome.FAILURE: "execution_failed",
    ExecutionOutcome.UNKNOWN: "execution_unknown",
}


class TaskNotRunningError(Exception):
    def __init__(self, task_id: str, actual_state: TaskState) -> None:
        self.task_id = task_id
        self.actual_state = actual_state
        super().__init__(
            f"Task {task_id!r} is not READY (currently {actual_state.value!r}); "
            "execution can only start after authorization and before receipt acceptance."
        )


class MissingDecisionError(Exception):
    """Raised if a task reaches execution without a persisted Decision.

    DecisionService persists a Decision before execution, but
    ExecutionService checks instead of trusting that invariant silently,
    per research doc Section 15.27's determinism requirement for
    security-sensitive components.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(
            f"Task {task_id!r} is RUNNING but has no persisted Decision; "
            "cannot determine authorized capabilities."
        )


class DuplicateExecutionSuppressedError(Exception):
    """Raised instead of re-invoking a tool handler when a prior
    execution with the same idempotency key already reported SUCCESS or
    UNKNOWN.

    Research doc Section 8.15 / Core Principle #17: "Provider retry must
    not automatically imply tool re-execution" — a SUCCESS means the
    side effect already happened; UNKNOWN means it might have (Appendix
    B: EXECUTION SUCCESS != VERIFIED SUCCESS cuts both ways — an
    unconfirmed outcome is not license to just try again). A prior
    FAILURE is NOT suppressed: by definition no side effect is claimed
    to have occurred, so a genuine retry is safe to attempt.
    """

    def __init__(
        self,
        task_id: str,
        tool: str,
        action: str,
        idempotency_key: str,
        prior_outcome: ExecutionOutcome,
    ) -> None:
        self.task_id = task_id
        self.tool = tool
        self.action = action
        self.idempotency_key = idempotency_key
        self.prior_outcome = prior_outcome
        super().__init__(
            f"Task {task_id!r} tool {tool!r}.{action!r} already has a "
            f"{prior_outcome.value} execution on record for idempotency "
            f"key {idempotency_key!r}; refusing to re-invoke the handler "
            "rather than risk duplicating a side effect that may have "
            "already occurred."
        )


@dataclass(frozen=True)
class ExecutionOutcomeRecord:
    response: ToolResponse
    outcome: ExecutionOutcome
    task: Task


class ExecutionService:
    """Technical doc Component: Execution Runtime, restricted to the
    "run one already-authorized, security-cleared tool call and advance
    the task accordingly" slice of that responsibility. The actual
    sandboxing mechanics (timeout, redaction) live in whatever
    ExecutionBoundary the injected ToolExecutor uses — this service does
    not implement isolation itself, it sequences the pipeline stages
    around it.

    `security_service` is REQUIRED, not optional. Phases 1-7 defaulted it
    to None (skipping the check) so tests could construct ExecutionService
    without one — that made security enforcement a convention rather than
    a mechanical guarantee, the exact gap the Phase 8 audit flagged: "the
    comment explicitly says production wiring should provide it, but
    that's a convention, not an architectural guarantee." There is no
    execute() path that can run a tool without a SecurityService.enforce()
    call happening first.

    Looks up the task's authorized capabilities from the persisted
    Decision (never from a caller-supplied value), so a capability check
    here reflects what was actually authorized, not what someone claims
    was authorized.

    It receives a READY task and does not transition it to RUNNING until
    the supervisor has accepted a durable execution receipt.

    Also computes an `IdempotencyKeyGenerator` key from (task, tool,
    action, arguments) before every attempt and checks it against prior
    `ToolExecutionRecord`s. A prior SUCCESS or UNKNOWN for the same key
    raises `DuplicateExecutionSuppressedError` instead of invoking the
    handler again — same gap class as `security_service` above:
    `IdempotencyKeyGenerator` existed, was unit-tested, and was never
    actually called from here until Phase 8.
    """

    def __init__(
        self,
        *,
        permit_authority: ExecutionPermitAuthority,
        tool_executor: ToolExecutor,
        execution_supervisor: ExecutionSupervisor,
        orchestrator: Orchestrator,
        decision_repository: DecisionRepository,
        authorization_repository: AuthorizationRepository,
        plan_repository: PlanRepository,
        tool_execution_repository: ToolExecutionRepository,
        audit: AuditRecorder,
        id_generator: IdGenerator,
        security_service: SecurityService,
        unit_of_work: UnitOfWork,
        clock: Clock,
        intent_repository: IntentRepository | None = None,
        credential_broker: CredentialBroker | None = None,
        execution_channel: ExecutionChannel | None = None,
    ) -> None:
        self._channel = execution_channel
        self._permits = permit_authority
        self._tool_executor = tool_executor
        self._execution_supervisor = execution_supervisor
        self._orchestrator = orchestrator
        self._decisions = decision_repository
        self._tool_executions = tool_execution_repository
        self._authorizations = authorization_repository
        self._plans = plan_repository
        self._audit = audit
        self._ids = id_generator
        self._security = security_service
        self._intent_repo = intent_repository
        self._uow = unit_of_work
        self._clock = clock
        self._credential_broker = credential_broker
        self._permit_authority = permit_authority or ExecutionPermitAuthority()
        self._execution_supervisor.ensure_adapter(
            ToolExecutorAdapter(
                tool_executor, 
                permit_authority=permit_authority,
                credential_broker=credential_broker,
                )
        )
        
    @staticmethod
    def _map_app_outcome(outcome: object) -> ExecutionOutcome:
        value = getattr(outcome, "value", str(outcome))

        mapping = {
            "SUCCESS": ExecutionOutcome.SUCCESS,
            "FAILURE": ExecutionOutcome.FAILURE,
            "UNKNOWN": ExecutionOutcome.UNKNOWN,
        }

        try:
            return mapping[str(value).upper()]
        except KeyError as exc:
            raise RuntimeError(
                f"Unsupported app execution outcome: {value!r}"
            ) from exc

    def execute(self, task: Task, request: ToolRequest) -> ExecutionOutcomeRecord:
        start_time = time.perf_counter()
        if task.state is not TaskState.READY:
            raise TaskNotRunningError(task.id, task.state)

        self._verify_authorization_binding(task, request)

        # Same logical operation (task/tool/action/arguments) always
        # produces the same key, so a retry of an already-attempted call
        # is detectable before the handler is ever reached — not just
        # unit-tested in isolation (see Phase 8 review for the gap this
        # closes).
        idempotency_key = IdempotencyKeyGenerator.generate(
            task_id=task.id,
            tool=request.tool,
            action=request.action,
            arguments=request.arguments,
        )
        prior = self._tool_executions.get_by_idempotency_key(idempotency_key)
        if prior is not None and prior.outcome in (
            ExecutionOutcome.SUCCESS,
            ExecutionOutcome.UNKNOWN,
        ):
            self._audit.record(
                actor="execution_service",
                component="execution.tool",
                event_type="execution.duplicate_suppressed",
                action=request.action,
                result="suppressed",
                request_id=task.request_id,
                task_id=task.id,
                metadata={
                    "tool": request.tool,
                    "idempotency_key": idempotency_key,
                    "prior_outcome": prior.outcome.value,
                },
            )
            raise DuplicateExecutionSuppressedError(
                task.id, request.tool, request.action, idempotency_key, prior.outcome
            )

        decision = self._decisions.get_by_task(task.id)
        if decision is None:
            raise MissingDecisionError(task.id)

        authorized_capabilities = frozenset(decision.capabilities)

        from shea.tools.executor import CapabilityNotAuthorizedError
        try:
            self._tool_executor.validate_capabilities(request, authorized_capabilities)
        except CapabilityNotAuthorizedError as exc:
            self._audit.record(
                actor="execution_service",
                component="execution.tool",
                event_type="execution.capability_denied",
                action=request.action,
                result="denied",
                request_id=task.request_id,
                task_id=task.id,
                metadata={
                    "tool": request.tool,
                    "missing_capabilities": sorted(exc.missing_capabilities),
                },
            )
            raise

        # Profile is frozen into the task for credential binding.
        # Secrets are NOT resolved here — only CredentialReference markers
        # travel on the contract. Broker injects at adapter invoke.
        profile_id = "system"
        if task.profile_snapshot and "profile_id" in task.profile_snapshot:
            profile_id = task.profile_snapshot["profile_id"]

        authorization = self._authorizations.list_by_task(task.id)[-1]
        permit = self._tool_executor.mint_permit(
            tool=request.tool,
            action=request.action,
            arguments=request.arguments,  # refs only — never secrets
            capabilities=authorized_capabilities,
        )
        permit_dict = {
            "token": permit.token,
            "tool": permit.tool,
            "action": permit.action,
            "arguments_digest": permit.arguments_digest,
            "capabilities_digest": permit.capabilities_digest,
        }

        contract = ExecutionContract(
            contract_id=self._ids.new_id(),
            authorization_id=authorization.id,
            capability=(
                sorted(authorized_capabilities)[0]
                if authorized_capabilities
                else f"tool.{request.tool}"
            ),
            operation=f"tool.{request.tool}.{request.action}",
            target=request.tool,
            arguments=dict(request.arguments),  # refs only
            expires_at=authorization.expires_at,
            requested_target=RequestedTarget(
                kind=IdentityKind.OPAQUE,
                value=request.tool,
            ),
            identity_requirements=IdentityRequirements(
                assurance=IdentityAssurance.BASIC,
                require_revalidation_before_invoke=False,
                allowed_kinds=frozenset({
                    IdentityKind.OPAQUE,
                    IdentityKind.PATH,
                    IdentityKind.URL,
                    IdentityKind.HOST,
                    IdentityKind.PROCESS,
                }),
            ),
            metadata={
                "execution_backend": "tool_executor",
                "tool": request.tool,
                "action": request.action,
                "request_id": request.request_id,
                "task_id": task.id,
                "tool_context": dict(request.context),
                "_authorized_capabilities": tuple(sorted(authorized_capabilities)),
                "_execution_permit": permit_dict,
                "_profile_id": profile_id,
                "idempotency_key": idempotency_key,
            },
        )

        def accept_receipt(receipt: ExecutionReceipt) -> None:
            nonlocal task
            task = self._orchestrator.advance(
                task.id,
                "authorize_and_run",
                receipt_id=receipt.id,
            )
            # Security enforcement remains a hard gate, but now follows
            # durable receipt acceptance and still precedes adapter invocation.
            self._security.enforce(task, request)

        supervision = self._execution_supervisor.execute(
            contract,
            on_receipt_accepted=accept_receipt,
        )
        raw_evidence = supervision.adapter_result.evidence or {}
        # ToolExecutorAdapter puts handler payload under "data"; native adapters may not
        evidence_data = raw_evidence.get("data", raw_evidence)

        app_verification_result = (
            supervision.verification.result.value
            if supervision.verification is not None
            else None
        )

        response = ToolResponse(
            success=self._map_app_outcome(supervision.adapter_result.outcome)
            is ExecutionOutcome.SUCCESS,
            data=evidence_data,
            error=supervision.adapter_result.error,
            metadata={
                "receipt_id": supervision.receipt.id,
                "attempt_id": supervision.attempt.id,
                "evidence_id": supervision.evidence_id,
                "app_verification_result": app_verification_result,
                "app_verification_explanation": (
                    supervision.verification.explanation
                    if supervision.verification is not None
                    else None
                ),
            },
        )

        scan = self._security.scan_output(task, request.tool, evidence_data)
        if scan.flagged:
            self._audit.record(
                actor="execution_service",
                component="execution.tool",
                event_type="execution.output_flagged",
                action=request.action,
                result="flagged",
                request_id=task.request_id,
                task_id=task.id,
                metadata={"reason": getattr(scan, "reason", None)},
            )
            response = ToolResponse(
                success=False,
                data=evidence_data,
                error=f"output scan flagged: {getattr(scan, 'reason', 'injection')}",
                metadata=response.metadata,
            )

        result = ExecutionOutcomeRecord(
            response=response,
            outcome=self._map_app_outcome(
                supervision.adapter_result.outcome
            ),
            task=task,
        )
        with self._uow:
            self._tool_executions.save(
                ToolExecutionRecord(
                    id=self._ids.new_id(),
                    task_id=task.id,
                    tool=request.tool,
                    action=request.action,
                    outcome=result.outcome,
                    success=result.response.success,
                    data=result.response.data,
                    error=result.response.error,
                    idempotency_key=idempotency_key,
                    metadata=result.response.metadata,
                )
            )

            self._audit.record(
                actor="execution_service",
                component="execution.tool",
                event_type=f"execution.{result.outcome.value.lower()}",
                action=request.action,
                result=result.outcome.value.lower(),
                request_id=task.request_id,
                task_id=task.id,
                metadata={
                    "tool": request.tool,
                    "success": result.response.success,
                    "receipt_id": supervision.receipt.id,
                    "attempt_id": supervision.attempt.id,
                    "evidence_id": supervision.evidence_id,
                },
            )
            
            if self._channel is not None:
                self._channel.completed(
                    task_id=task.id,
                    tool=request.tool,
                    outcome=result.outcome,
                    request_id=task.request_id,
                    data=result.response.data,
                )

            # Consume one-shot auth only on durable SUCCESS
            if result.outcome is ExecutionOutcome.SUCCESS:
                auths = self._authorizations.list_by_task(task.id)
                if auths:
                    auth = auths[-1]
                    if auth.nonce is not None and auth.used_at is None:
                        auth.used_at = self._clock.now()
                        self._authorizations.save(auth)

            advanced_task = self._orchestrator.advance(
                task.id,
                _ADVANCE_EVENT_BY_OUTCOME[result.outcome],
            )

        duration_ms = (time.perf_counter() - start_time) * 1000
        global_metrics.record_tool_execution(
            duration_ms=duration_ms,
            success=(result.outcome is ExecutionOutcome.SUCCESS)
        )
        return ExecutionOutcomeRecord(
            response=result.response, outcome=result.outcome, task=advanced_task
        )
        
    def _verify_authorization_binding(self, task: Task, request: ToolRequest) -> None:
        """Verify that the task's most recent authorization covers this request.

        The rules live in `check_authorization_binding` (shared with the
        supervisor's independent check); this only supplies the records.
        """
        authorizations = self._authorizations.list_by_task(task.id)
        if not authorizations:
            raise MissingDecisionError(task.id)

        check_authorization_binding(
            task=task,
            auth=authorizations[-1],
            plan=self._plans.get_by_task(task.id),
            tool=request.tool,
            arguments=request.arguments,
            now=self._clock.now(),
        )