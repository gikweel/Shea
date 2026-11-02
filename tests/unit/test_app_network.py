from __future__ import annotations

from shea.app.adapters.network_local import LocalNetworkAdapter
from shea.app.contracts import ExecutionContract
from shea.app.enums import AppOutcome
from shea.app.identities import IdentityKind, RequestedTarget
from shea.app.memory import (
    InMemoryAttemptRepository,
    InMemoryReceiptRepository,
)
from shea.app.scopes import ExecutionScope, NetworkScope
from shea.app.supervisor import ExecutionSupervisor
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier


def test_network_requires_scope(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    supervisor = ExecutionSupervisor(
        adapters=[LocalNetworkAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    result = supervisor.execute(
        ExecutionContract(
            contract_id="c-net-1",
            authorization_id="a1",
            capability="network.request",
            operation="network.request",
            target="https://example.com/",
            arguments={"url": "https://example.com/", "method": "GET"},
            # no scope
        )
    )
    assert result.adapter_result.outcome is AppOutcome.FAILURE
    assert "scope" in (result.adapter_result.error or "").lower()


def test_network_blocks_localhost(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    scope = ExecutionScope(
        scope_id="net",
        network=NetworkScope(allowed_hosts=None, block_private=True),
    )
    supervisor = ExecutionSupervisor(
        adapters=[LocalNetworkAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    result = supervisor.execute(
        ExecutionContract(
            contract_id="c-net-local",
            authorization_id="a1",
            capability="network.request",
            operation="network.request",
            target="http://127.0.0.1/",
            arguments={"url": "http://127.0.0.1/", "method": "GET"},
            requested_target=RequestedTarget(
                kind=IdentityKind.URL, value="http://127.0.0.1/"
            ),
            scope=scope,
        )
    )
    assert result.adapter_result.outcome is AppOutcome.FAILURE