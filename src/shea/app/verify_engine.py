from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from shea.app.contracts import AdapterResult
from shea.app.enums import AppOutcome, EvidenceStrength
from shea.app.evidence import EvidenceRecord
from shea.app.verification import (
    AppVerificationRecord,
    VerificationPolicy,
    evidence_kind_for_operation,
)


def _hash_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def evidence_from_adapter(
    *,
    evidence_id: str,
    receipt_id: str,
    attempt_id: str,
    operation: str,
    result: AdapterResult,
    observed_at: datetime,
) -> EvidenceRecord:
    kind = evidence_kind_for_operation(operation)
    payload: dict[str, Any] = {
        "outcome": result.outcome.value,
        "error": result.error,
        **dict(result.evidence),
    }
    return EvidenceRecord(
        id=evidence_id,
        receipt_id=receipt_id,
        attempt_id=attempt_id,
        kind=kind,
        source="adapter",
        strength=EvidenceStrength.DIRECT,
        observed_at=observed_at,
        content_hash=_hash_payload(payload),
        payload=payload,
    )


def evaluate_verification(
    *,
    verification_id: str,
    receipt_id: str,
    attempt_id: str,
    policy: VerificationPolicy,
    adapter_result: AdapterResult,
    evidence: EvidenceRecord,
    verified_at: datetime,
) -> AppVerificationRecord:
    """Independent of adapter success: check required postcondition markers."""
    explanation_parts: list[str] = []
    outcome = adapter_result.outcome

    # Missing evidence can only ever *prevent success* or leave an outcome
    # indeterminate; it can never establish failure. So an adapter-reported
    # UNKNOWN stays UNKNOWN here — downgrading it to FAILURE would license a
    # blind retry of an action that may already have happened.
    missing = (
        AppOutcome.UNKNOWN
        if adapter_result.outcome is AppOutcome.UNKNOWN
        else policy.missing_evidence_outcome
    )

    if policy.required_evidence_kinds and evidence.kind not in policy.required_evidence_kinds:
        outcome = missing
        explanation_parts.append(
            f"missing required evidence kind (have {evidence.kind})"
        )

    if evidence.strength.value not in {
        EvidenceStrength.DIRECT.value,
        EvidenceStrength.DERIVED.value,
        EvidenceStrength.INFERRED.value,
    }:
        pass  # enum already constrained

    # Strength gate
    strength_order = {
        EvidenceStrength.INFERRED: 1,
        EvidenceStrength.DERIVED: 2,
        EvidenceStrength.DIRECT: 3,
    }
    if strength_order[evidence.strength] < strength_order[policy.minimum_strength]:
        outcome = missing
        explanation_parts.append("evidence strength below policy minimum")

    if policy.requires_transport_verification:
        transport_complete = evidence.payload.get("transport_complete")
        status = evidence.payload.get("status")
        tls_verified = evidence.payload.get("tls_verified")
        transport_verified = (
            transport_complete is True
            and isinstance(status, int)
            and not isinstance(status, bool)
            and 200 <= status < 300
            and tls_verified is not False
        )
        if not transport_verified:
            outcome = missing
            explanation_parts.append("transport not verified")

    post = evidence.payload.get("postcondition")
    for required in policy.postconditions:
        if post == required:
            continue
        # Also accept explicit flags adapters already set
        if required == "exists" and evidence.payload.get("path"):
            if adapter_result.outcome is AppOutcome.SUCCESS and (
                evidence.payload.get("created")
                or evidence.payload.get("bytes_written") is not None
                or post in {"content_match", "exists"}
            ):
                continue
        if required == "absent" and (
            post == "absent"
            or evidence.payload.get("deleted") is True
            or evidence.payload.get("note") == "already absent"
        ):
            continue
        if required == "content_match" and post == "content_match":
            continue
        if required == "source_and_destination_exist" and post == "source_and_destination_exist":
            continue
        if required == "source_absent_destination_exists" and post == "source_absent_destination_exists":
            continue

        # Required postcondition not satisfied
        if adapter_result.outcome is AppOutcome.SUCCESS:
            outcome = (
                AppOutcome.UNKNOWN
                if policy.contradiction_behavior == "unknown"
                else AppOutcome.FAILURE
            )
            explanation_parts.append(f"postcondition not met: {required}")
        elif adapter_result.outcome is AppOutcome.UNKNOWN:
            outcome = AppOutcome.UNKNOWN
            explanation_parts.append(
                f"unknown and postcondition unchecked: {required}"
            )

    if not explanation_parts:
        explanation_parts.append(
            "policy satisfied" if outcome is AppOutcome.SUCCESS else outcome.value
        )

    return AppVerificationRecord(
        id=verification_id,
        receipt_id=receipt_id,
        attempt_id=attempt_id,
        policy_id=policy.policy_id,
        expected_postconditions=policy.postconditions,
        evidence_ids=(evidence.id,),
        result=outcome,
        explanation="; ".join(explanation_parts),
        verified_at=verified_at,
    )


# ---------------------------------------------------------------------------
# GAP 2 — Concrete UNKNOWN outcome protocol
# Reference this whenever outcome == AppOutcome.UNKNOWN.
# ---------------------------------------------------------------------------
UNKNOWN_PROTOCOL = {
    "triggers": [
        "Side effect initiated but connection dropped before result received",
        "Adapter returned no outcome field",
        "Timeout waiting for adapter response without explicit FAILURE",
        "Partial side effect observed but completeness uncertain",
        "External system acknowledged intent but returned ambiguous status",
    ],
    "must_not_do": [
        "Never collapse UNKNOWN into FAILURE for non-idempotent actions",
        "Never invent certainty — UNKNOWN means still unknowable",
        "Never retry without re-verifying the original side effect status",
        "Never route UNKNOWN straight to FAILED — route to BLOCKED instead",
        "Never use UNKNOWN as a shortcut for 'we don't know what happened'",
    ],
    "recovery_steps": [
        "Route to BLOCKED state (needs investigation)",
        "Attempt re-verification with different evidence",
        "If idempotent: may retry with exponential backoff",
        "If non-idempotent: require explicit user acknowledgement",
        "Log as UNKNOWN_OUTCOME in audit records",
    ],
}