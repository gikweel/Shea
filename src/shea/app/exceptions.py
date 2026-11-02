from __future__ import annotations


class AppPlaneError(Exception):
    pass


class ContractValidationError(AppPlaneError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Execution contract invalid: {reason}")


class ReceiptRequiredError(AppPlaneError):
    """Raised if an adapter would run without a durable receipt."""

    def __init__(self, contract_id: str) -> None:
        self.contract_id = contract_id
        super().__init__(
            f"Refusing invoke for contract {contract_id!r}: no durable receipt"
        )

class NoSideEffectError(AppPlaneError):
    """An adapter raises this — and only this — when it can *establish*
    that it had not yet done anything external when it failed (a rejected
    argument, a missing prerequisite, a policy gate).

    The supervisor treats it as a definite FAILURE. Every other exception
    out of `adapter.invoke()` is treated as UNKNOWN, because by then the
    attempt is already INVOKED and an exception proves nothing about
    whether a side effect happened (research doc 12.13: "the operation
    succeeds, the connection drops, Shea receives no result" is UNKNOWN,
    not failure). Do not raise this after any external effect.
    """


class AuthorizationRejectedError(ContractValidationError):
    """The supervisor's own authorization check refused the contract.

    Raised before any receipt or attempt exists, so a rejected contract
    leaves no execution record and never reaches an adapter.
    """