from __future__ import annotations

from dataclasses import dataclass, field

from shea.contracts.enums import RiskLevel

# Same idea as policy.py's capability sets: these are configurable
# defaults using the vocabulary from technical doc Section 10.4, not
# hardcoded business logic baked into the engine.
DEFAULT_ELEVATED_PRIVILEGE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "process.execute",
        "filesystem.write",
        "credential.access",
        "system.configure",
        "network.listen",
    }
)
DEFAULT_NETWORK_CAPABILITIES: frozenset[str] = frozenset({"network.connect", "network.outbound"})

# Factor count -> RiskLevel. Doc Section 12's worked example lists exactly
# four factors (elevated privileges, external network interaction,
# irreversible modification, untrusted source) for a HIGH classification;
# this engine uses those same four factors and maps factor count onto the
# six-level scale so the mapping is total, not just illustrative.
_LEVEL_BY_FACTOR_COUNT: dict[int, RiskLevel] = {
    0: RiskLevel.SAFE,
    1: RiskLevel.LOW,
    2: RiskLevel.MEDIUM,
    3: RiskLevel.HIGH,
}


@dataclass(frozen=True)
class RiskFactors:
    """Structured risk inputs — deliberately not a free-text description.
    Each field maps directly to one of technical doc Section 12's
    consequence categories.
    """

    capabilities: frozenset[str] = field(default_factory=frozenset[str])
    reversible: bool = True
    external_content_involved: bool = False


@dataclass(frozen=True)
class RiskAssessmentResult:
    level: RiskLevel
    factors: list[str]
    explanation: str


@dataclass(frozen=True)
class RiskEngine:
    """Produces a classification AND an explanation, never a bare score —
    technical doc Section 12: "The risk subsystem must provide both a
    classification and an explanation."
    """

    elevated_privilege_capabilities: frozenset[str] = field(
        default_factory=lambda: DEFAULT_ELEVATED_PRIVILEGE_CAPABILITIES
    )
    network_capabilities: frozenset[str] = field(
        default_factory=lambda: DEFAULT_NETWORK_CAPABILITIES
    )

    def assess(self, inputs: RiskFactors) -> RiskAssessmentResult:
        factors: list[str] = []

        if inputs.capabilities & self.elevated_privilege_capabilities:
            factors.append("Elevated privileges")
        if inputs.capabilities & self.network_capabilities:
            factors.append("External network interaction")
        if not inputs.reversible:
            factors.append("Irreversible modification")
        if inputs.external_content_involved:
            factors.append("Untrusted source")

        level = _LEVEL_BY_FACTOR_COUNT.get(len(factors), RiskLevel.CRITICAL)

        if factors:
            explanation = "Risk factors present: " + "; ".join(factors) + "."
        else:
            explanation = "No risk factors identified."

        return RiskAssessmentResult(level=level, factors=factors, explanation=explanation)


# ---------------------------------------------------------------------------
# GAP 3 — V1 scope boundary audit
# Call audit_v1_scope("feature_name") before implementing any new feature.
# If it returns False, defer to V2/V3.
# ---------------------------------------------------------------------------
V1_BOUNDARY_LIST = frozenset(
    {
        "task_creation",
        "state_transition",
        "receipt_management",
        "basic_transition",
        "illegal_transition_detection",
        "audit_logging",
        "task_cancellation",
        "terminal_state_enforcement",
        "transition_validation",
        "clock_integration",
        # Explicitly NOT in V1 (commented for documentation):
        # "model_inference", "model_decision", "model_authority_override",
        # "model_invoke_adapter", "complex_risk_assessment",
        # "unknown_outcome_protocol", "phased_deployment",
    }
)

_V1_CORE = frozenset(
    {
        "task_lifecycle",
        "state_machine",
        "receipt_system",
        "transition_audit",
        "basic_risk",
    }
)


def audit_v1_scope(feature_name: str) -> bool:
    """Return True if feature is in V1 scope, False if it should wait for V2/V3."""
    if feature_name in V1_BOUNDARY_LIST:
        return True
    if feature_name in _V1_CORE:
        return True
    return False
