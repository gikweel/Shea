from __future__ import annotations

from datetime import UTC, datetime

from shea.app.adapters.stub import StubAdapter
from shea.app.contracts import (
    AdapterResult,
    ExecutionAttempt,
    ExecutionContract,
    ExecutionReceipt,
)
from shea.app.enums import AppOutcome, AttemptState, ReceiptState
from shea.app.memory import (
    InMemoryAttemptRepository,
    InMemoryReceiptRepository,
)
from shea.app.supervisor import ExecutionSupervisor
from tests.helper import AllowTestAuthorizationVerifier


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def new_id(self) -> str:
        self._n += 1
        return f"app-{self._n}"


class _Uow:
    def __enter__(self) -> _Uow:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_supervisor_persists_receipt_before_success() -> None:
    receipts = InMemoryReceiptRepository()
    attempts = InMemoryAttemptRepository()
    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=receipts,  # type: ignore[arg-type]
        attempt_repository=attempts,  # type: ignore[arg-type]
        clock=_Clock(),  # type: ignore[arg-type]
        id_generator=_Ids(),  # type: ignore[arg-type]
        unit_of_work=_Uow(),  # type: ignore[arg-type]
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    contract = ExecutionContract(
        contract_id="c1",
        authorization_id="a1",
        capability="stub",
        operation="stub.echo",
        target="unit-test",
    )
    result = supervisor.execute(contract)

    assert result.receipt.state is ReceiptState.FINALIZED
    assert result.attempt.state is AttemptState.FINALIZED
    assert result.adapter_result.outcome is AppOutcome.SUCCESS
    assert receipts.get(result.receipt.id) is not None
    assert result.adapter_result.evidence["receipt_id"] == result.receipt.id


class _RaisingAdapter:
    """Simulates an adapter that raises instead of returning a structured
    AdapterResult — e.g. an unexpected internal error, or (as with
    LocalProcessAdapter._check_executable_allowed) a validation check
    that raises rather than returning FAILURE itself.
    """

    name = "raising"

    def supports(self, contract: ExecutionContract) -> bool:
        return contract.operation.startswith("raising.")

    def invoke(
        self, contract: ExecutionContract, receipt: ExecutionReceipt, attempt: ExecutionAttempt
    ) -> AdapterResult:
        raise RuntimeError("adapter blew up unexpectedly")


def test_supervisor_finalizes_receipt_and_attempt_when_adapter_raises_unexpectedly() -> None:
    """Before this fix, an adapter raising instead of returning an
    AdapterResult left the receipt stuck in ATTEMPTING and the attempt
    stuck in INVOKED forever — no outcome, no error recorded, nothing to
    reconcile against. Confirmed by direct reproduction, not just code
    review. execute() must not itself raise in this case either: the
    exception is captured into an AdapterResult and returned normally.
    The outcome is UNKNOWN, not FAILURE: the attempt was already INVOKED,
    so an unexpected exception does not prove no side effect happened
    (see test_app_supervisor_exception_outcomes.py for the full matrix).
    """
    receipts = InMemoryReceiptRepository()
    attempts = InMemoryAttemptRepository()
    supervisor = ExecutionSupervisor(
        adapters=[_RaisingAdapter()],
        receipt_repository=receipts,  # type: ignore[arg-type]
        attempt_repository=attempts,  # type: ignore[arg-type]
        clock=_Clock(),  # type: ignore[arg-type]
        id_generator=_Ids(),  # type: ignore[arg-type]
        unit_of_work=_Uow(),  # type: ignore[arg-type]
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    contract = ExecutionContract(
        contract_id="c1",
        authorization_id="a1",
        capability="raising",
        operation="raising.boom",
        target="unit-test",
    )

    result = supervisor.execute(contract)

    assert result.receipt.state is ReceiptState.FINALIZED
    assert result.attempt.state is AttemptState.FINALIZED
    assert result.adapter_result.outcome is AppOutcome.UNKNOWN
    assert "adapter blew up unexpectedly" in (result.adapter_result.error or "")
    stored_receipt = receipts.get(result.receipt.id)
    assert stored_receipt is not None
    assert stored_receipt.state is ReceiptState.FINALIZED
    assert stored_receipt.outcome is AppOutcome.UNKNOWN