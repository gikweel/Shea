from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC

from shea.app.adapters.base import Adapter
from shea.app.contracts import (
    AdapterResult,
    ExecutionAttempt,
    ExecutionContract,
    ExecutionReceipt,
    effective_requested_target,
)
from shea.app.enums import (
    AppOutcome,
    AttemptState,
    IdempotencyState,
    ReceiptState,
    RecoveryStatus,
)
from shea.app.environment import probe_environment
from shea.app.exceptions import (
    AuthorizationRejectedError,
    ContractValidationError,
    NoSideEffectError,
    ReceiptRequiredError,
)
from shea.app.idempotency import IdempotencyRecord
from shea.app.identity import IdentityResolver, IdentityRevalidator
from shea.app.ports import (
    AppVerificationRepository,
    AttemptRepository,
    EvidenceRepository,
    IdempotencyRepository,
    ReceiptRepository,
    RecoveryRepository,
)
from shea.app.ports.authorization import AuthorizationVerifier
from shea.app.ports.process import AdapterContext
from shea.app.reconcile.registry import ReconcilerRegistry, default_reconciler_registry
from shea.app.recovery import (
    ReconciliationResult,
    RecoveryIncident,
    classify_stuck_attempt,
)
from shea.app.scope_enforce import evaluate_scope
from shea.app.scopes import ScopeEnforcementReport
from shea.app.verification import AppVerificationRecord, policy_for_operation
from shea.app.verify_engine import evaluate_verification, evidence_from_adapter
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class SupervisionResult:
    receipt: ExecutionReceipt
    attempt: ExecutionAttempt
    adapter_result: AdapterResult
    evidence_id: str | None = None
    verification: AppVerificationRecord | None = None


def _definite_no_effect_errors() -> tuple[type[Exception], ...]:
    """Exceptions that mean "stopped before anything external happened".

    `SecurityViolationError` is raised only by pre-effect policy gates
    (scope, path, destination, profile checks); an adapter must not raise
    it after performing an external effect. Resolved lazily because
    importing `shea.security` at module load would be circular
    (security -> tools -> execution -> app.supervisor).
    """
    from shea.security.exceptions import SecurityViolationError

    return (NoSideEffectError, SecurityViolationError)


# Operations that by definition mutate nothing, so an exception out of them
# can safely be reported as FAILURE rather than UNKNOWN.
_SIDE_EFFECT_FREE_OPERATIONS: frozenset[str] = frozenset(
    {"filesystem.read", "filesystem.inspect", "application.inspect"}
)


class ExecutionSupervisor:
    """Sole entry for privileged app-plane work.

    Ordering:
      validate → idempotency reserve → resolve/verify identity → scope →
      persist receipt+attempt → revalidate → INVOKED → invoke adapter →
      evidence + verification → finalize (outcome = verification result)
    No adapter call may precede a durable receipt.
    """

    def __init__(
        self,
        *,
        adapters: list[Adapter],
        receipt_repository: ReceiptRepository,
        attempt_repository: AttemptRepository,
        clock: Clock,
        id_generator: IdGenerator,
        unit_of_work: UnitOfWork,
        authorization_verifier: AuthorizationVerifier,
        identity_resolver: IdentityResolver | None = None,
        identity_revalidator: IdentityRevalidator | None = None,
        evidence_repository: EvidenceRepository | None = None,
        verification_repository: AppVerificationRepository | None = None,
        idempotency_repository: IdempotencyRepository | None = None,
        recovery_repository: RecoveryRepository | None = None,
        reconciler_registry: ReconcilerRegistry | None = None,
    ) -> None:
        self._adapters = adapters
        self._receipts = receipt_repository
        self._attempts = attempt_repository
        self._clock = clock
        self._ids = id_generator
        self._uow = unit_of_work
        self._authorization_verifier = authorization_verifier
        self._identity_resolver = identity_resolver or IdentityResolver()
        self._identity_revalidator = identity_revalidator or IdentityRevalidator()
        self._evidence_repo = evidence_repository
        self._verification_repo = verification_repository
        self._idempotency_repo = idempotency_repository
        self._recovery_repo = recovery_repository
        self._reconcilers = reconciler_registry or default_reconciler_registry()

    def ensure_adapter(self, adapter: Adapter) -> None:
        for index, existing in enumerate(self._adapters):
            if existing.name == adapter.name:
                self._adapters[index] = adapter
                return
        self._adapters.append(adapter)

    def execute(self, contract: ExecutionContract) -> SupervisionResult:
        self._validate(contract)
        self._verify_authorization(contract)
        adapter = self._select_adapter(contract)

        idem_key = contract.metadata.get("idempotency_key")
        if idem_key is not None:
            self._reserve_idempotency(str(idem_key))

        env = probe_environment(clock_now=self._clock.now())

        requested = effective_requested_target(contract)
        resolved = self._identity_resolver.resolve(
            requested, contract.identity_requirements
        )
        self._identity_revalidator.verify(
            resolved, contract.identity_requirements, phase="pre_receipt"
        )

        enforcement: ScopeEnforcementReport | None = None
        if contract.scope is not None:
            enforcement = evaluate_scope(contract.scope, contract)

        now = self._clock.now()
        receipt = ExecutionReceipt(
            id=self._ids.new_id(),
            contract_id=contract.contract_id,
            authorization_id=contract.authorization_id,
            capability=contract.capability,
            operation=contract.operation,
            target=contract.target,
            state=ReceiptState.CREATED,
            created_at=now,
            adapter_name=adapter.name,
            metadata=dict(contract.metadata),
        )
        attempt = ExecutionAttempt(
            id=self._ids.new_id(),
            receipt_id=receipt.id,
            attempt_number=1,
            state=AttemptState.CREATED,
            created_at=now,
            adapter_name=adapter.name,
        )

        with self._uow:
            self._receipts.save(receipt)
            receipt.state = ReceiptState.ATTEMPTING
            self._receipts.save(receipt)
            self._attempts.save(attempt)
            if idem_key is not None and self._idempotency_repo is not None:
                self._bind_idempotency_receipt(str(idem_key), receipt.id)

        if self._receipts.get(receipt.id) is None:
            raise ReceiptRequiredError(contract.contract_id)

        # Revalidate BEFORE marking INVOKED so a failed check does not
        # leave an attempt stuck in INVOKED with no finalize.
        verified = self._identity_revalidator.verify(
            resolved, contract.identity_requirements, phase="pre_invoke"
        )

        if contract.scope is not None and enforcement is not None:
            ctx = AdapterContext(
                verified_identity=verified,
                scope=contract.scope,
                enforcement=enforcement,
            )
            contract = ExecutionContract(
                contract_id=contract.contract_id,
                authorization_id=contract.authorization_id,
                capability=contract.capability,
                operation=contract.operation,
                target=contract.target,
                arguments=contract.arguments,
                contract_hash=contract.contract_hash,
                expires_at=contract.expires_at,
                requested_target=contract.requested_target,
                identity_requirements=contract.identity_requirements,
                scope=contract.scope,
                metadata={
                    **contract.metadata,
                    "_adapter_context": ctx,
                    "environment": env.platform,
                },
            )

        attempt.state = AttemptState.INVOKED
        attempt.invoked_at = self._clock.now()
        with self._uow:
            self._attempts.save(attempt)

        try:
            result = adapter.invoke(contract, receipt, attempt)
        except _definite_no_effect_errors() as exc:
            # The adapter (or a policy gate it called) is saying, by type,
            # that it stopped before doing anything external: a definite
            # FAILURE, safe to report as such.
            result = AdapterResult(
                outcome=AppOutcome.FAILURE,
                error=f"adapter rejected before any side effect: {exc}",
                evidence={
                    "receipt_id": receipt.id,
                    "attempt_id": attempt.id,
                    "adapter": adapter.name,
                    "side_effect_possible": False,
                    "exception_type": type(exc).__name__,
                },
            )
        except Exception as exc:  # noqa: BLE001 — adapters are untrusted code
            # Anything else raised after the attempt was marked INVOKED is
            # NOT proof that nothing happened: the process may have been
            # spawned, the write may have landed, the request may have been
            # accepted before the adapter tripped over its own bookkeeping.
            # Claiming FAILURE would license an unsafe retry of a possibly
            # non-idempotent action, so the outcome is UNKNOWN — except for
            # operations that cannot have side effects at all.
            effect_free = contract.operation in _SIDE_EFFECT_FREE_OPERATIONS
            result = AdapterResult(
                outcome=AppOutcome.FAILURE if effect_free else AppOutcome.UNKNOWN,
                error=f"adapter raised: {exc}",
                evidence={
                    "receipt_id": receipt.id,
                    "attempt_id": attempt.id,
                    "adapter": adapter.name,
                    "side_effect_possible": not effect_free,
                    "exception_type": type(exc).__name__,
                },
            )

        # Phase 4: evidence + independent verification
        policy = policy_for_operation(contract.operation)
        observed_at = self._clock.now()
        evidence = evidence_from_adapter(
            evidence_id=self._ids.new_id(),
            receipt_id=receipt.id,
            attempt_id=attempt.id,
            operation=contract.operation,
            result=result,
            observed_at=observed_at,
        )
        verification = evaluate_verification(
            verification_id=self._ids.new_id(),
            receipt_id=receipt.id,
            attempt_id=attempt.id,
            policy=policy,
            adapter_result=result,
            evidence=evidence,
            verified_at=observed_at,
        )

        final_outcome = verification.result
        if final_outcome is not result.outcome:
            result = AdapterResult(
                outcome=final_outcome,
                evidence=dict(result.evidence),
                error=result.error or verification.explanation,
            )

        attempt.state = AttemptState.FINALIZED
        attempt.finalized_at = self._clock.now()
        attempt.outcome = final_outcome
        attempt.error = result.error
        attempt.evidence = dict(result.evidence)

        receipt.state = ReceiptState.FINALIZED
        receipt.finalized_at = attempt.finalized_at
        receipt.outcome = final_outcome

        with self._uow:
            self._attempts.save(attempt)
            self._receipts.save(receipt)
            if self._evidence_repo is not None:
                self._evidence_repo.save(evidence)
            if self._verification_repo is not None:
                self._verification_repo.save(verification)
            if idem_key is not None:
                self._complete_idempotency(str(idem_key), receipt.id, final_outcome)

        return SupervisionResult(
            receipt=receipt,
            attempt=attempt,
            adapter_result=result,
            evidence_id=evidence.id,
            verification=verification,
        )

    def reconcile_receipt(self, receipt_id: str) -> ReconciliationResult:
        """Finalize stuck receipts without re-invoking adapters (no auto-retry)."""
        receipt = self._receipts.get(receipt_id)
        if receipt is None:
            raise ContractValidationError(f"unknown receipt {receipt_id!r}")

        attempts = self._attempts.list_by_receipt(receipt_id)
        attempt = attempts[-1] if attempts else None
        reason = classify_stuck_attempt(receipt, attempt)
        if reason is None:
            return ReconciliationResult(
                receipt=receipt,
                attempt=attempt,
                incident=None,
                outcome=receipt.outcome or AppOutcome.SUCCESS,
                detail="nothing_to_reconcile",
            )

        # INVOKED without finalize → side effect may have occurred
        observation = self._reconcilers.observe(
            receipt, attempt, stuck_reason=reason
        )
        outcome = observation.outcome
        now = self._clock.now()

        if attempt is not None and attempt.state is not AttemptState.FINALIZED:
            attempt.state = AttemptState.FINALIZED
            attempt.finalized_at = now
            attempt.outcome = outcome
            attempt.error = f"reconciled: {observation.detail}"
            if observation.evidence:
                merged = dict(attempt.evidence or {})
                merged.update(observation.evidence)
                merged["reconcile_detail"] = observation.detail
                merged["side_effect_detected"] = observation.side_effect_detected
                attempt.evidence = merged

        receipt.state = ReceiptState.FINALIZED
        receipt.finalized_at = now
        receipt.outcome = outcome

        incident: RecoveryIncident | None = None
        status = (
            RecoveryStatus.QUARANTINED
            if outcome is AppOutcome.UNKNOWN
            else RecoveryStatus.RESOLVED
        )
        if self._recovery_repo is not None:
            incident = RecoveryIncident(
                id=self._ids.new_id(),
                receipt_id=receipt.id,
                attempt_id=attempt.id if attempt else None,
                status=status,
                reason=reason,
                created_at=now,
                updated_at=now,
                resolution=f"finalized_as_{outcome.value}:{observation.detail}",
                fence_token=self._ids.new_id(),
            )

        with self._uow:
            if attempt is not None:
                self._attempts.save(attempt)
            self._receipts.save(receipt)
            if incident is not None and self._recovery_repo is not None:
                self._recovery_repo.save_incident(incident)

        return ReconciliationResult(
            receipt=receipt,
            attempt=attempt,
            incident=incident,
            outcome=outcome,
            detail=observation.detail,
        )
        
    def reconcile_stuck(self) -> list[ReconciliationResult]:
        """Startup / maintenance sweep: finalize stuck receipts without re-invoke."""
        results: list[ReconciliationResult] = []
        for receipt in self._receipts.list_unfinalized():
            results.append(self.reconcile_receipt(receipt.id))
        return results

    def _reserve_idempotency(self, key: str) -> None:
        if self._idempotency_repo is None:
            return
        existing = self._idempotency_repo.get(key)
        if existing is not None:
            if existing.state is IdempotencyState.COMPLETED:
                raise ContractValidationError(f"idempotency key already completed: {key}")
            if existing.state is IdempotencyState.IN_PROGRESS:
                raise ContractValidationError(f"idempotency key in progress: {key}")
            if existing.state is IdempotencyState.CONFLICT:
                raise ContractValidationError(f"idempotency key conflict: {key}")
        now = self._clock.now()
        record = IdempotencyRecord(
            key=key,
            receipt_id=None,
            state=IdempotencyState.IN_PROGRESS,
            outcome=None,
            created_at=now,
            updated_at=now,
        )
        reserve = getattr(self._idempotency_repo, "reserve", None)
        if reserve is not None:
            try:
                reserve(record)
            except Exception as exc:
                raise ContractValidationError(
                    f"idempotency key already reserved: {key}"
                ) from exc
        else:
            with self._uow:
                self._idempotency_repo.save(record)

    def _bind_idempotency_receipt(self, key: str, receipt_id: str) -> None:
        if self._idempotency_repo is None:
            return
        existing = self._idempotency_repo.get(key)
        now = self._clock.now()
        if existing is None:
            self._idempotency_repo.save(
                IdempotencyRecord(
                    key=key,
                    receipt_id=receipt_id,
                    state=IdempotencyState.IN_PROGRESS,
                    outcome=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            return
        existing.receipt_id = receipt_id
        existing.updated_at = now
        self._idempotency_repo.save(existing)

    def _complete_idempotency(
        self, key: str, receipt_id: str, outcome: AppOutcome
    ) -> None:
        if self._idempotency_repo is None:
            return
        now = self._clock.now()
        state = (
            IdempotencyState.COMPLETED
            if outcome is AppOutcome.SUCCESS
            else IdempotencyState.UNKNOWN
            if outcome is AppOutcome.UNKNOWN
            else IdempotencyState.FAILED
        )
        existing = self._idempotency_repo.get(key)
        created = existing.created_at if existing is not None else now
        self._idempotency_repo.save(
            IdempotencyRecord(
                key=key,
                receipt_id=receipt_id,
                state=state,
                outcome=outcome,
                created_at=created,
                updated_at=now,
            )
        )

    def _verify_authorization(self, contract: ExecutionContract) -> None:
        """Independently confirm the contract is covered by a live,
        matching authorization — before any receipt exists, so a refused
        contract leaves no execution record and never reaches an adapter.

        Fail closed: if the verifier cannot answer (storage error, a bug),
        that is a refusal, not a pass. If mandatory security checks cannot
        be performed, privileged execution must stop."""
        try:
            self._authorization_verifier.verify(contract)
        except Exception as exc:  # noqa: BLE001 — any failure to verify is a refusal
            raise AuthorizationRejectedError(
                f"authorization {contract.authorization_id!r} rejected: {exc}"
            ) from exc

    def _validate(self, contract: ExecutionContract) -> None:
        if not contract.contract_id:
            raise ContractValidationError("missing contract_id")
        if not contract.authorization_id:
            raise ContractValidationError("missing authorization_id")
        if not contract.capability:
            raise ContractValidationError("missing capability")
        if not contract.operation:
            raise ContractValidationError("missing operation")
        if not contract.target:
            raise ContractValidationError("missing target")
        if contract.expires_at is not None:
            now = self._clock.now()
            exp = contract.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if now > exp:
                raise ContractValidationError("contract expired")

    def _select_adapter(self, contract: ExecutionContract) -> Adapter:
        preferred_name = contract.metadata.get("execution_backend")
        if isinstance(preferred_name, str):
            for adapter in self._adapters:
                if adapter.name == preferred_name and adapter.supports(contract):
                    return adapter

        for adapter in self._adapters:
            if adapter.supports(contract):
                return adapter
        raise ContractValidationError(
            f"no adapter supports operation {contract.operation!r}"
            + (f" (preferred: {preferred_name!r})" if preferred_name else "")
        )