from __future__ import annotations

import os

import pytest

from shea.app.adapters.network_local import LocalNetworkAdapter
from shea.app.contracts import ExecutionContract
from shea.app.enums import AppOutcome, ReceiptState
from shea.app.identities import (
    IdentityAssurance,
    IdentityKind,
    IdentityRequirements,
    RequestedTarget,
)
from shea.app.memory import (
    InMemoryAttemptRepository,
    InMemoryReceiptRepository,
)
from shea.app.scopes import ExecutionScope, NetworkScope, ResourceLimits
from shea.app.supervisor import ExecutionSupervisor
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier

pytestmark = pytest.mark.skipif(
    os.environ.get("SHEA_LIVE_NETWORK") != "1",
    reason="Set SHEA_LIVE_NETWORK=1 to run live network demo",
)


def test_ep_network_get_example_com(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    scope = ExecutionScope(
        scope_id="net-demo",
        network=NetworkScope(
            allowed_hosts=frozenset({"example.com", "www.example.com"}),
            block_private=True,
        ),
        resources=ResourceLimits(wall_time_ms=15_000, output_bytes=500_000),
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
            contract_id="c-net-demo",
            authorization_id="auth-demo",
            capability="network.request",
            operation="network.request",
            target="https://example.com/",
            arguments={
                "url": "https://example.com/",
                "method": "GET",
                "max_redirects": 3,
            },
            requested_target=RequestedTarget(
                kind=IdentityKind.URL,
                value="https://example.com/",
            ),
            identity_requirements=IdentityRequirements(
                assurance=IdentityAssurance.BASIC
            ),
            scope=scope,
        )
    )
    assert result.receipt.state is ReceiptState.FINALIZED
    assert result.adapter_result.outcome is AppOutcome.SUCCESS
    assert result.adapter_result.evidence.get("status") == 200
    assert "example" in (
        result.adapter_result.evidence.get("untrusted_content_block") or ""
    ).lower()