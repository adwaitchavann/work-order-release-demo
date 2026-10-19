"""The signed receipt that binds a determination to one exact action, evidence set, policy and work order record.

SIMULATED: receipts are signed with HMAC-SHA256 and a demo key. See README,
"Limitations", for why a production system would use asymmetric signatures.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

from .canonical import canonical_json

SIGNATURE_ALGORITHM = "HMAC-SHA256 (SIMULATED demo key)"


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    verdict: str
    work_order_id: str
    action_digest: str
    issued_at: str   # ISO-8601, UTC
    expires_at: str  # ISO-8601, UTC
    issuer: str
    key_id: str
    simulated: bool
    signature: str
    # Bindings. A receipt missing any of these is refused by the executor.
    evidence_digest: str | None = None   # digest of the evidence the verdict was based on
    policy_version: str | None = None    # declared version of the policy applied
    policy_digest: str | None = None     # digest of that policy's content
    facts_digest: str | None = None      # digest of the MES record facts the verdict relied on

    def signed_fields(self) -> dict[str, Any]:
        """Every field except the signature. Changing any of them invalidates the signature."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "signature"}

    def signing_payload(self) -> bytes:
        return canonical_json(self.signed_fields())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Receipt":
        return cls(**d)


def compute_mac(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()
