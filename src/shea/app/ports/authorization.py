from __future__ import annotations

from typing import Protocol

from shea.app.contracts import ExecutionContract


class AuthorizationVerifier(Protocol):
    """Independently confirms that a contract is covered by a live,
    matching authorization.

    The app plane receives contracts from a caller it must not simply
    trust. An `authorization_id` string proves nothing on its own, so the
    supervisor asks this port — implemented against the persisted
    authorization record — before it creates a receipt.

    Implementations raise on rejection (any exception rejects: the
    supervisor fails closed) and return None on success. They must not
    mutate the authorization; consuming a one-shot authorization stays
    with whoever observed the durable outcome.
    """

    def verify(self, contract: ExecutionContract) -> None: ...