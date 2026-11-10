"""The supervisor must not take an `authorization_id` on trust.

These run the real `RecordAuthorizationVerifier` against real persisted
records. Every refusal must happen before a receipt exists, before the
task is told it may run, and before any adapter is invoked.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shea.app.contracts import (
    AdapterResult,
    ExecutionAttempt,
    ExecutionContract,
    ExecutionReceipt,
)
from shea.app.enums import AppOutcome
from shea.app.exceptions import AuthorizationRejectedError
from shea.app.supervisor import ExecutionSupervisor
from shea.contracts.models import Authorization, Plan, PlanStep, Task
from shea.core.orchestrator import Orchestrator
from shea.execution.authorization import AuthorizationRejected, RecordAuthorizationVerifier
from shea.persistence.sqlite.authorization_repository import SqliteAuthorizationRepository
from shea.persistence.sqlite.plan_repository import SqlitePlanRepository
from shea.persistence.sqlite.task_repository import SqliteTaskRepository
from shea.security.binding import (
    compute_arguments_hash,
    compute_plan_hash,
    compute_step_hash,
)
from shea.security.exceptions import (
    AuthorizationAlreadyUsedError,
    AuthorizationBindingMismatchError,
    AuthorizationExpiredError,
)
from tests.conftest import FrozenClock

ARGS: dict[str, Any] = {"path": "note.txt", "content": "hello"}
FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


class _Scenario:
    def __init__(
        self,
        orchestrator: Orchestrator,
        plans: SqlitePlanRepository,
        authorizations: SqliteAuthorizationRepository,
        tasks: SqliteTaskRepository,
        clock: FrozenClock,
    ) -> None:
        self.orchestrator = orchestrator
        self.plans = plans
        self.authorizations = authorizations
        self.verifier = RecordAuthorizationVerifier(
            authorizations=authorizations, tasks=tasks, plans=plans, clock=clock
        )
        self.task: Task = orchestrator.create_task(session_id="s", request_id="req")
        step = PlanStep(
            id="step-1", plan_id="plan-1", order=1, description="write",
            tool="filesystem.write", arguments=dict(ARGS),
        )  # fmt: skip
        self.plan = Plan(id="plan-1", task_id=self.task.id, objective="o", steps=[step])
        plans.save(self.plan)
        self.auth = self.grant()

    def grant(self, **overrides: Any) -> Authorization:
        fields: dict[str, Any] = {
            "id": f"auth-{len(self.authorizations.list_by_task(self.task.id)) + 1}",
            "task_id": self.task.id,
            "profile_id": "system",
            "granted": True,
            "granted_by": "user",
            "explicit": True,
            "plan_hash": compute_plan_hash(self.plan),
            "step_hash": compute_step_hash(self.plan.steps[0]),
            "arguments_hash": compute_arguments_hash(ARGS),
            "expires_at": FUTURE,
            "used_at": None,
            "nonce": "nonce-1",
        }
        fields.update(overrides)
        auth = Authorization(**fields)
        self.authorizations.save(auth)
        return auth

    def contract(self, **overrides: Any) -> ExecutionContract:
        fields: dict[str, Any] = {
            "contract_id": "c1",
            "authorization_id": self.auth.id,
            "capability": "filesystem.write",
            "operation": "tool.filesystem.write.write",
            "target": "filesystem.write",
            "arguments": dict(ARGS),
            "expires_at": self.auth.expires_at,
            "metadata": {"task_id": self.task.id, "tool": "filesystem.write"},
        }
        fields.update(overrides)
        return ExecutionContract(**fields)


@pytest.fixture
def scenario(
    orchestrator: Orchestrator,
    plan_repository: SqlitePlanRepository,
    authorization_repository: SqliteAuthorizationRepository,
    task_repository: SqliteTaskRepository,
    clock: FrozenClock,
) -> _Scenario:
    return _Scenario(orchestrator, plan_repository, authorization_repository, task_repository, clock)


def test_a_live_matching_authorization_is_accepted(scenario: _Scenario) -> None:
    scenario.verifier.verify(scenario.contract())


def test_unknown_authorization_id_is_refused(scenario: _Scenario) -> None:
    with pytest.raises(AuthorizationRejected, match="unknown authorization"):
        scenario.verifier.verify(scenario.contract(authorization_id="made-up"))


def test_an_authorization_from_another_task_cannot_be_replayed(scenario: _Scenario) -> None:
    other = scenario.orchestrator.create_task(session_id="s", request_id="req2")
    contract = scenario.contract(metadata={"task_id": other.id, "tool": "filesystem.write"})
    with pytest.raises(AuthorizationRejected, match="different task"):
        scenario.verifier.verify(contract)


def test_an_ungranted_authorization_is_refused(scenario: _Scenario) -> None:
    scenario.auth = scenario.grant(granted=False, nonce="n2")
    with pytest.raises(AuthorizationRejected, match="not granted"):
        scenario.verifier.verify(scenario.contract())


def test_a_superseded_authorization_is_refused(scenario: _Scenario) -> None:
    old = scenario.auth
    scenario.grant(nonce="n2")  # a newer authorization now exists for the task
    with pytest.raises(AuthorizationRejected, match="superseded"):
        scenario.verifier.verify(scenario.contract(authorization_id=old.id))


def test_tampered_arguments_are_refused(scenario: _Scenario) -> None:
    contract = scenario.contract(arguments={"path": "/etc/passwd", "content": "x"})
    with pytest.raises(AuthorizationBindingMismatchError):
        scenario.verifier.verify(contract)


def test_a_different_tool_than_the_one_authorized_is_refused(scenario: _Scenario) -> None:
    contract = scenario.contract(metadata={"task_id": scenario.task.id, "tool": "process.run"})
    with pytest.raises(AuthorizationBindingMismatchError):
        scenario.verifier.verify(contract)


def test_a_consumed_authorization_cannot_be_reused(scenario: _Scenario) -> None:
    scenario.auth = replace(scenario.auth, used_at=datetime(2026, 1, 1, tzinfo=UTC))
    scenario.authorizations.save(scenario.auth)
    with pytest.raises(AuthorizationAlreadyUsedError):
        scenario.verifier.verify(scenario.contract())


def test_an_expired_authorization_is_refused(scenario: _Scenario) -> None:
    scenario.auth = scenario.grant(expires_at=datetime(2025, 1, 1, tzinfo=UTC), nonce="n2")
    with pytest.raises(AuthorizationExpiredError):
        scenario.verifier.verify(scenario.contract())


def test_a_contract_cannot_outlive_its_authorization(scenario: _Scenario) -> None:
    with pytest.raises(AuthorizationRejected, match="outlives"):
        scenario.verifier.verify(scenario.contract(expires_at=FUTURE + timedelta(days=1)))
    with pytest.raises(AuthorizationRejected, match="outlives"):
        scenario.verifier.verify(scenario.contract(expires_at=None))


def test_a_contract_with_no_task_provenance_is_refused(scenario: _Scenario) -> None:
    with pytest.raises(AuthorizationRejected, match="provenance"):
        scenario.verifier.verify(scenario.contract(metadata={"tool": "filesystem.write"}))


class _CountingAdapter:
    name = "counting"

    def __init__(self) -> None:
        self.invocations = 0

    def supports(self, contract: ExecutionContract) -> bool:
        return True

    def invoke(
        self, contract: ExecutionContract, receipt: ExecutionReceipt, attempt: ExecutionAttempt
    ) -> AdapterResult:
        self.invocations += 1
        return AdapterResult(outcome=AppOutcome.SUCCESS)


@pytest.fixture
def repos(
    clock: FrozenClock,
    id_generator: Any,
    unit_of_work: Any,
    app_receipt_repository: Any,
    app_attempt_repository: Any,
) -> dict[str, Any]:
    return {
        "receipt_repository": app_receipt_repository,
        "attempt_repository": app_attempt_repository,
        "clock": clock,
        "id_generator": id_generator,
        "unit_of_work": unit_of_work,
    }


def _supervisor(adapter: _CountingAdapter, verifier: Any, repos: dict[str, Any]) -> ExecutionSupervisor:
    return ExecutionSupervisor(adapters=[adapter], authorization_verifier=verifier, **repos)


def test_a_refused_contract_leaves_no_receipt_never_runs_the_task_and_never_reaches_an_adapter(
    scenario: _Scenario, repos: dict[str, Any]
) -> None:
    adapter = _CountingAdapter()
    accepted: list[ExecutionReceipt] = []
    supervisor = _supervisor(adapter, scenario.verifier, repos)

    with pytest.raises(AuthorizationRejectedError, match="made-up"):
        supervisor.execute(
            scenario.contract(authorization_id="made-up"), on_receipt_accepted=accepted.append
        )

    assert adapter.invocations == 0
    assert accepted == []  # the task was never told it may move to RUNNING
    assert repos["receipt_repository"].list_unfinalized() == []


def test_the_supervisor_fails_closed_when_the_verifier_itself_breaks(
    scenario: _Scenario, repos: dict[str, Any]
) -> None:
    class _Broken:
        def verify(self, contract: ExecutionContract) -> None:
            raise RuntimeError("database is locked")

    adapter = _CountingAdapter()
    accepted: list[ExecutionReceipt] = []
    supervisor = _supervisor(adapter, _Broken(), repos)

    with pytest.raises(AuthorizationRejectedError, match="database is locked"):
        supervisor.execute(scenario.contract(), on_receipt_accepted=accepted.append)

    assert adapter.invocations == 0
    assert accepted == []
    assert repos["receipt_repository"].list_unfinalized() == []


def test_a_valid_contract_runs_when_the_real_verifier_approves(
    scenario: _Scenario, repos: dict[str, Any]
) -> None:
    adapter = _CountingAdapter()
    accepted: list[ExecutionReceipt] = []
    supervisor = _supervisor(adapter, scenario.verifier, repos)

    result = supervisor.execute(scenario.contract(), on_receipt_accepted=accepted.append)

    assert adapter.invocations == 1
    assert result.receipt.outcome is AppOutcome.SUCCESS
    assert len(accepted) == 1  # the durable receipt was announced before the adapter ran
