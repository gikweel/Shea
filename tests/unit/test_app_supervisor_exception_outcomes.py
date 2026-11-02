"""An exception out of `adapter.invoke()` is not proof that nothing happened.

By the time invoke() runs, the attempt is INVOKED. Research doc 12.13: the
operation may have succeeded and the adapter then failed collecting the
result — that is UNKNOWN, and UNKNOWN must not license a blind retry.
"""

from __future__ import annotations

import pytest

from shea.app.contracts import (
    AdapterResult,
    ExecutionAttempt,
    ExecutionContract,
    ExecutionReceipt,
)
from shea.app.enums import AppOutcome, AttemptState, IdempotencyState, ReceiptState
from shea.app.exceptions import ContractValidationError, NoSideEffectError
from shea.app.supervisor import ExecutionSupervisor
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from shea.security.exceptions import SecurityViolationError
from tests.helper import AllowTestAuthorizationVerifier


class _RaisingAdapter:
    name = "raising"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.invocations = 0

    def supports(self, contract: ExecutionContract) -> bool:
        return True

    def invoke(
        self, contract: ExecutionContract, receipt: ExecutionReceipt, attempt: ExecutionAttempt
    ) -> AdapterResult:
        self.invocations += 1
        raise self._exc


def _contract(operation: str, **metadata: object) -> ExecutionContract:
    return ExecutionContract(
        contract_id="c1",
        authorization_id="a1",
        capability=operation.split(".")[0],
        operation=operation,
        target="unit-test",
        metadata=dict(metadata),
    )


def _supervisor(
    adapter: _RaisingAdapter,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    receipts: object,
    attempts: object,
    **extra: object,
) -> ExecutionSupervisor:
    return ExecutionSupervisor(
        adapters=[adapter],
        receipt_repository=receipts,  # type: ignore[arg-type]
        attempt_repository=attempts,  # type: ignore[arg-type]
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
        **extra,  # type: ignore[arg-type]
    )


def test_unexpected_exception_on_a_mutating_operation_is_unknown(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: object,
    app_attempt_repository: object,
) -> None:
    adapter = _RaisingAdapter(OSError("connection reset while reading result"))
    supervisor = _supervisor(
        adapter, clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )

    result = supervisor.execute(_contract("process.execute"))

    assert result.adapter_result.outcome is AppOutcome.UNKNOWN
    assert result.adapter_result.evidence["side_effect_possible"] is True
    assert result.adapter_result.evidence["exception_type"] == "OSError"
    assert result.receipt.state is ReceiptState.FINALIZED
    assert result.receipt.outcome is AppOutcome.UNKNOWN
    assert result.attempt.state is AttemptState.FINALIZED
    assert result.attempt.outcome is AppOutcome.UNKNOWN


@pytest.mark.parametrize("operation", ["filesystem.read", "filesystem.inspect", "application.inspect"])
def test_unexpected_exception_on_a_side_effect_free_operation_is_failure(
    operation: str,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: object,
    app_attempt_repository: object,
) -> None:
    adapter = _RaisingAdapter(OSError("disk error"))
    supervisor = _supervisor(
        adapter, clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )

    result = supervisor.execute(_contract(operation))

    assert result.adapter_result.outcome is AppOutcome.FAILURE
    assert result.adapter_result.evidence["side_effect_possible"] is False


@pytest.mark.parametrize(
    "exc",
    [
        NoSideEffectError("argument rejected"),
        SecurityViolationError("fs", "filesystem", "path outside roots"),
    ],
    ids=["NoSideEffectError", "SecurityViolationError"],
)
def test_declared_pre_effect_rejection_is_a_definite_failure(
    exc: Exception,
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: object,
    app_attempt_repository: object,
) -> None:
    adapter = _RaisingAdapter(exc)
    supervisor = _supervisor(
        adapter, clock, id_generator, unit_of_work, app_receipt_repository, app_attempt_repository
    )

    result = supervisor.execute(_contract("process.execute"))

    assert result.adapter_result.outcome is AppOutcome.FAILURE
    assert result.adapter_result.evidence["side_effect_possible"] is False
    assert result.receipt.outcome is AppOutcome.FAILURE


def test_unknown_from_an_exception_blocks_a_blind_retry_of_the_same_operation(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: object,
    app_attempt_repository: object,
    app_idempotency_repository: object,
) -> None:
    """The point of UNKNOWN: the second attempt must not re-run an action
    that may already have happened."""
    adapter = _RaisingAdapter(OSError("timeout after request was sent"))
    supervisor = _supervisor(
        adapter,
        clock,
        id_generator,
        unit_of_work,
        app_receipt_repository,
        app_attempt_repository,
        idempotency_repository=app_idempotency_repository,
    )
    contract = _contract("network.request", idempotency_key="pay-invoice-42")

    first = supervisor.execute(contract)
    assert first.adapter_result.outcome is AppOutcome.UNKNOWN
    recorded = app_idempotency_repository.get("pay-invoice-42")  # type: ignore[attr-defined]
    assert recorded.state is IdempotencyState.UNKNOWN

    with pytest.raises(ContractValidationError, match="already reserved"):
        supervisor.execute(contract)
    assert adapter.invocations == 1  # the adapter was never run a second time


def test_a_definite_failure_is_still_distinguishable_from_unknown_in_idempotency(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: object,
    app_attempt_repository: object,
    app_idempotency_repository: object,
) -> None:
    adapter = _RaisingAdapter(NoSideEffectError("bad argument"))
    supervisor = _supervisor(
        adapter,
        clock,
        id_generator,
        unit_of_work,
        app_receipt_repository,
        app_attempt_repository,
        idempotency_repository=app_idempotency_repository,
    )

    supervisor.execute(_contract("process.execute", idempotency_key="k1"))

    recorded = app_idempotency_repository.get("k1")  # type: ignore[attr-defined]
    assert recorded.state is IdempotencyState.FAILED
