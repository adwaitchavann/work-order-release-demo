"""Verifier side: checks receipt signatures. This is what the executor is given; it exposes no way to sign."""

from __future__ import annotations

import hmac
from typing import Mapping

from .receipts import Receipt, compute_mac


class ReceiptVerifier:
    """SIMULATED verifier for HMAC-SHA256 demo receipts."""

    def __init__(self, keys: Mapping[str, bytes]) -> None:
        self._keys = dict(keys)

    def check_signature(self, receipt: Receipt) -> str | None:
        """Return None if the signature is valid, otherwise a short reason."""
        secret = self._keys.get(receipt.key_id)
        if secret is None:
            return f"unknown signing key {receipt.key_id!r}"
        if not isinstance(receipt.signature, str) or not receipt.signature:
            return "receipt has no signature"
        try:
            expected = compute_mac(secret, receipt.signing_payload())
        except (TypeError, ValueError):
            return "receipt contents are malformed"
        if not hmac.compare_digest(expected, receipt.signature):
            return "signature does not match the receipt contents"
        return None
