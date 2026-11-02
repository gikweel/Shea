from __future__ import annotations

import pytest

from shea.app.adapters.stub import StubAdapter
from shea.app.contracts import ExecutionContract
from shea.app.enums import IdempotencyState
from shea.app.exceptions import ContractValidationError
from shea.app.idempotency import IdempotencyRecord
from shea.app.memory import (
    InMemoryAttemptRepository,
    InMemoryReceiptRepository,
)
from shea.app.supervisor import ExecutionSupervisor
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier


class InMemoryIdempotencyRepo:
    def __init__(self) -> None:
        self._items: dict[str, IdempotencyRecord] = {}

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._items.get(key)

    def save(self, record: IdempotencyRecord) -> None:
        self._items[record.key] = record


def test_idempotency_in_progress_blocks_concurrent_key(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    repo = InMemoryIdempotencyRepo()
    now = clock.now()
    repo.save(
        IdempotencyRecord(
            key="busy",
            receipt_id="r-other",
            state=IdempotencyState.IN_PROGRESS,
            outcome=None,
            created_at=now,
            updated_at=now,
        )
    )
    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        idempotency_repository=repo,  # type: ignore[arg-type]
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    with pytest.raises(ContractValidationError, match="in progress"):
        supervisor.execute(
            ExecutionContract(
                contract_id="c1",
                authorization_id="a1",
                capability="stub",
                operation="stub.echo",
                target="t",
                metadata={"idempotency_key": "busy"},
            )
        )