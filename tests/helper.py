from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shea.app.contracts import ExecutionContract, ExecutionReceipt
from shea.app.enums import ReceiptState
from shea.app.ports import ReceiptRepository
from shea.contracts.models import Task
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator

MINIMAL_SCHEMA: dict[str, Mapping[str, Any]] = {
    "_unused": {"type": "any", "required": False, "default": None},
}


class AllowTestAuthorizationVerifier:
    """Accept contracts in isolated app-supervisor tests."""

    def verify(self, contract: ExecutionContract) -> None:
        pass


def persist_accepted_receipt_for_test(
    repository: ReceiptRepository,
    task: Task,
    clock: Clock,
    id_generator: IdGenerator,
) -> str:
    receipt_id = id_generator.new_id()
    repository.save(
        ExecutionReceipt(
            id=receipt_id,
            contract_id=id_generator.new_id(),
            authorization_id="test-authorization",
            capability="test",
            operation="test.operation",
            target="test",
            state=ReceiptState.ATTEMPTING,
            created_at=clock.now(),
            metadata={"task_id": task.id},
        )
    )
    return receipt_id