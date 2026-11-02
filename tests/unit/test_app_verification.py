from __future__ import annotations

from shea.app.adapters.stub import StubAdapter
from shea.app.contracts import AdapterResult, ExecutionContract
from shea.app.enums import AppOutcome, EvidenceStrength
from shea.app.memory import (
    InMemoryAttemptRepository,
    InMemoryReceiptRepository,
)
from shea.app.supervisor import ExecutionSupervisor
from shea.app.verification import policy_for_operation
from shea.app.verify_engine import evaluate_verification, evidence_from_adapter
from shea.ports.clock import Clock
from shea.ports.id_generator import IdGenerator
from shea.ports.unit_of_work import UnitOfWork
from tests.helper import AllowTestAuthorizationVerifier


def test_policy_for_filesystem_write() -> None:
    policy = policy_for_operation("filesystem.write")
    assert policy.policy_id == "fs.write.v1"
    assert "content_match" in policy.postconditions


def test_evaluate_verification_passes_when_postcondition_present(
    clock: Clock, id_generator: IdGenerator
) -> None:
    now = clock.now()
    adapter_result = AdapterResult(
        outcome=AppOutcome.SUCCESS,
        evidence={"postcondition": "content_match", "path": "/tmp/x"},
    )
    evidence = evidence_from_adapter(
        evidence_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        operation="filesystem.write",
        result=adapter_result,
        observed_at=now,
    )
    assert evidence.kind == "adapter.filesystem"
    assert evidence.strength is EvidenceStrength.DIRECT

    verification = evaluate_verification(
        verification_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        policy=policy_for_operation("filesystem.write"),
        adapter_result=adapter_result,
        evidence=evidence,
        verified_at=now,
    )
    assert verification.result is AppOutcome.SUCCESS


def test_evaluate_verification_fails_missing_postcondition(
    clock: Clock, id_generator: IdGenerator
) -> None:
    now = clock.now()
    adapter_result = AdapterResult(
        outcome=AppOutcome.SUCCESS,
        evidence={"path": "/tmp/x"},  # no postcondition marker
    )
    evidence = evidence_from_adapter(
        evidence_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        operation="filesystem.write",
        result=adapter_result,
        observed_at=now,
    )
    verification = evaluate_verification(
        verification_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        policy=policy_for_operation("filesystem.write"),
        adapter_result=adapter_result,
        evidence=evidence,
        verified_at=now,
    )
    assert verification.result is AppOutcome.FAILURE
    assert "postcondition" in verification.explanation


def test_network_transport_requires_complete_verified_evidence(
    clock: Clock, id_generator: IdGenerator
) -> None:
    now = clock.now()
    adapter_result = AdapterResult(
        outcome=AppOutcome.SUCCESS,
        evidence={"postcondition": "response_received", "status": 200},
    )
    evidence = evidence_from_adapter(
        evidence_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        operation="network.request",
        result=adapter_result,
        observed_at=now,
    )

    verification = evaluate_verification(
        verification_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        policy=policy_for_operation("network.request"),
        adapter_result=adapter_result,
        evidence=evidence,
        verified_at=now,
    )

    assert verification.result is AppOutcome.FAILURE
    assert "transport not verified" in verification.explanation


def test_network_transport_verifies_2xx_without_claiming_business_success(
    clock: Clock, id_generator: IdGenerator
) -> None:
    now = clock.now()
    adapter_result = AdapterResult(
        outcome=AppOutcome.SUCCESS,
        evidence={
            "postcondition": "response_received",
            "transport_complete": True,
            "status": 204,
            "tls_verified": None,
            "business_success": None,
        },
    )
    evidence = evidence_from_adapter(
        evidence_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        operation="http.fetch",
        result=adapter_result,
        observed_at=now,
    )

    verification = evaluate_verification(
        verification_id=id_generator.new_id(),
        receipt_id="r1",
        attempt_id="a1",
        policy=policy_for_operation("http.fetch"),
        adapter_result=adapter_result,
        evidence=evidence,
        verified_at=now,
    )

    assert evidence.kind == "adapter.network"
    assert verification.result is AppOutcome.SUCCESS
    assert evidence.payload["business_success"] is None


def test_supervisor_attaches_verification_without_repos(
    clock: Clock,
    id_generator: IdGenerator,
    unit_of_work: UnitOfWork,
    app_receipt_repository: InMemoryReceiptRepository,
    app_attempt_repository: InMemoryAttemptRepository,
) -> None:
    supervisor = ExecutionSupervisor(
        adapters=[StubAdapter()],
        receipt_repository=app_receipt_repository,
        attempt_repository=app_attempt_repository,
        clock=clock,
        id_generator=id_generator,
        unit_of_work=unit_of_work,
        authorization_verifier=AllowTestAuthorizationVerifier(),
    )
    result = supervisor.execute(
        ExecutionContract(
            contract_id="c-v1",
            authorization_id="auth-1",
            capability="stub",
            operation="stub.echo",
            target="unit",
        )
    )
    assert result.verification is not None
    assert result.evidence_id is not None
    assert result.receipt.outcome is AppOutcome.SUCCESS
    assert result.verification.result is AppOutcome.SUCCESS