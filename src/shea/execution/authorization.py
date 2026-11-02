from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from shea.app.contracts import ExecutionContract
from shea.contracts.models import Authorization, Plan, Task
from shea.ports.clock import Clock
from shea.ports.repositories import (
    AuthorizationRepository,
    PlanRepository,
    TaskRepository,
)
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


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def check_authorization_binding(
    *,
    task: Task,
    auth: Authorization,
    plan: Plan | None,
    tool: str,
    arguments: Mapping[str, Any],
    now: datetime,
) -> None:
    """The one rule for "does this authorization cover this request?".

    Used by `ExecutionService` before it builds a contract and, through
    `RecordAuthorizationVerifier`, by the supervisor before it creates a
    receipt — so the two paths cannot drift into enforcing different
    things. Raises on any violation; returns None when the authorization
    is live and bound to exactly this plan, step and arguments.

      1. The authorization belongs to the task's profile
      2. It has not been consumed (replay protection)
      3. It has not expired
      4. Plan hash matches      (when the authorization carries one)
      5. Step hash matches      (when the authorization carries one)
      6. Arguments hash matches (when the authorization carries one)
    """
    active_profile_id = "system"
    if task.profile_snapshot and "profile_id" in task.profile_snapshot:
        active_profile_id = task.profile_snapshot["profile_id"]
    if auth.profile_id != active_profile_id and auth.profile_id != "system":
        raise AuthorizationAlreadyUsedError(task.id, "Profile mismatch - Security Violation")

    if auth.used_at is not None:
        raise AuthorizationAlreadyUsedError(task.id, auth.nonce or "")

    if auth.expires_at is not None and _aware(auth.expires_at) < _aware(now):
        raise AuthorizationExpiredError(task.id, auth.expires_at)

    if auth.plan_hash is not None:
        if plan is None:
            raise AuthorizationBindingMismatchError(task.id, "plan", auth.plan_hash, None)
        current_plan_hash = compute_plan_hash(plan)
        if auth.plan_hash != current_plan_hash:
            raise AuthorizationBindingMismatchError(
                task.id, "plan", auth.plan_hash, current_plan_hash
            )

    if auth.step_hash is not None:
        if plan is None:
            raise AuthorizationBindingMismatchError(task.id, "step", auth.step_hash, None)
        matching_step = next(
            (s for s in plan.steps if s.tool == tool and compute_step_hash(s) == auth.step_hash),
            None,
        )
        if matching_step is None:
            # No step has this exact hash; compute a candidate for diagnostics.
            candidate = next((s for s in plan.steps if s.tool == tool), None)
            candidate_hash = compute_step_hash(candidate) if candidate else None
            raise AuthorizationBindingMismatchError(
                task.id, "step", auth.step_hash, candidate_hash
            )

    if auth.arguments_hash is not None:
        current_arguments_hash = compute_arguments_hash(dict(arguments))
        if auth.arguments_hash != current_arguments_hash:
            raise AuthorizationBindingMismatchError(
                task.id, "arguments", auth.arguments_hash, current_arguments_hash
            )


class AuthorizationRejected(Exception):
    """The persisted authorization does not cover this contract."""


class RecordAuthorizationVerifier:
    """App-plane `AuthorizationVerifier` backed by the persisted records.

    Everything is looked up from storage — nothing the caller put in the
    contract is believed except the identifiers needed to find the
    records. A contract naming someone else's authorization, a superseded
    one, an ungranted one, or one whose bound arguments differ is refused.
    """

    def __init__(
        self,
        *,
        authorizations: AuthorizationRepository,
        tasks: TaskRepository,
        plans: PlanRepository,
        clock: Clock,
    ) -> None:
        self._authorizations = authorizations
        self._tasks = tasks
        self._plans = plans
        self._clock = clock

    def verify(self, contract: ExecutionContract) -> None:
        task_id = contract.metadata.get("task_id")
        tool = contract.metadata.get("tool")
        if not isinstance(task_id, str) or not task_id:
            raise AuthorizationRejected("contract carries no task provenance")
        if not isinstance(tool, str) or not tool:
            raise AuthorizationRejected("contract names no tool")

        auth = self._authorizations.get(contract.authorization_id)
        if auth is None:
            raise AuthorizationRejected(f"unknown authorization {contract.authorization_id!r}")
        if auth.task_id != task_id:
            raise AuthorizationRejected("authorization was granted for a different task")
        if not auth.granted:
            raise AuthorizationRejected("authorization was not granted")

        task = self._tasks.get(task_id)
        if task is None:
            raise AuthorizationRejected(f"unknown task {task_id!r}")

        history = self._authorizations.list_by_task(task_id)
        if not history or history[-1].id != auth.id:
            raise AuthorizationRejected("authorization has been superseded by a newer one")

        # A contract must not outlive the authority it was issued under.
        if auth.expires_at is not None and (
            contract.expires_at is None or _aware(contract.expires_at) > _aware(auth.expires_at)
        ):
            raise AuthorizationRejected("contract outlives its authorization")

        check_authorization_binding(
            task=task,
            auth=auth,
            plan=self._plans.get_by_task(task_id),
            tool=tool,
            arguments=contract.arguments,
            now=self._clock.now(),
        )