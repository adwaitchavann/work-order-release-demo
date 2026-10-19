"""Issuer side: signs receipts. Only the determiner uses this module; the executor never imports it."""

from __future__ import annotations

from dataclasses import replace

from .receipts import Receipt, compute_mac


class ReceiptSigner:
    """SIMULATED signer: HMAC-SHA256 with a demo key.

    It signs whatever it is given. Enforcing that a receipt carries its evidence,
    policy and facts bindings is the executor's job, so a receipt signed without
    them is still refused at release time.
    """

    def __init__(self, key_id: str, secret: bytes, issuer: str = "mock-determiner (SIMULATED)") -> None:
        self.key_id = key_id
        self._secret = secret
        self.issuer = issuer

    def sign(
        self,
        *,
        receipt_id: str,
        verdict: str,
        work_order_id: str,
        action_digest: str,
        issued_at: str,
        expires_at: str,
        evidence_digest: str | None = None,
        policy_version: str | None = None,
        policy_digest: str | None = None,
        facts_digest: str | None = None,
    ) -> Receipt:
        unsigned = Receipt(
            receipt_id=receipt_id,
            verdict=verdict,
            work_order_id=work_order_id,
            action_digest=action_digest,
            issued_at=issued_at,
            expires_at=expires_at,
            issuer=self.issuer,
            key_id=self.key_id,
            simulated=True,
            signature="",
            evidence_digest=evidence_digest,
            policy_version=policy_version,
            policy_digest=policy_digest,
            facts_digest=facts_digest,
        )
        return replace(unsigned, signature=compute_mac(self._secret, unsigned.signing_payload()))
