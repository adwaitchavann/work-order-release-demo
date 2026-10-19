# Work order release demo (SIMULATED)

> **Everything here is simulated.** Determinations come from a mock determiner, receipts are signed with a demo HMAC key, and the MES and evidence systems are in-memory stand-ins. All data is synthetic; no customer data is used.

An AI planning agent proposes releasing a manufacturing work order. Each proposal goes through four stages:

**proposed action → evidence → determination → effect**

Deciding and doing are separate. The mock determiner decides and issues a signed receipt. A separate executor, the only component that can release a work order, verifies that receipt itself before acting. **ADMIT permits the executor to attempt the release; it does not mean the release succeeded.** The executor reports its own outcome: `RELEASED`, `FAILED` or `REFUSED`.

## Setup

Python 3.10 or later. There are no runtime dependencies; pytest is only needed for the tests.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
```

## Run

From the repository root:

```bash
python -m wo_demo run                                  # all six cases, four-stage trace
python -m wo_demo run --case 03_halt_revoked_approval  # one case
python -m wo_demo run --json out/trace.json            # also write a JSON trace
python -m wo_demo gate                                 # the executor refusing misuse (17 steps)
python -m wo_demo list                                 # list the cases
python -m pytest -v                                    # 179 tests
python -m pytest tests/test_review_gaps.py -v          # the 11 review regression tests
```

## Cases

Fixtures are in `fixtures/`: the synthetic policy, the MES work order records and one JSON file per case.

| Case | Situation | Verdict | Effect |
|---|---|---|---|
| `01_admit_release` | Active approval, scope matches the MES record, all checks pass | ADMIT | Released |
| `02_admit_execution_failure` | Same, but the MES has the work order locked | ADMIT | Failed (`WORK_ORDER_LOCKED`); not released; receipt spent |
| `03_halt_revoked_approval` | The only supervisor approval was revoked | HALT | Refused, no release |
| `04_defer_missing_evidence` | Quality-hold check not provided | DEFER | Refused, no release |
| `05_refer_quality_signoff` | Safety-critical part on the MES record; no Quality Engineering sign-off | REFER | Refused, no release |
| `06_admit_reissued_approval` | Approval revoked, then a new approval issued | ADMIT | Released |

## How it works

### Decide: the mock determiner (`determiner.py`)

The verdicts are the four core verdicts, unchanged (`verdicts.py`).

**Facts come from the MES record, not the proposal.** The determiner looks the work order up through a read-only reader (it cannot release anything). Part number, revision, line and planned quantity are read from the record. Safety-critical status is decided from the record's part, and approval scopes are compared against the record. The proposal's own claims about plant, line, part and revision are only compared against the record, and any disagreement is a HALT (`FACT_MISMATCH`).

**Checks fail closed.** A required check passes only if its value is exactly `true`. Absent or `null` is DEFER (`MISSING_EVIDENCE`), `false` is HALT (`CHECK_FAILED`), and anything else (`"FAIL"`, `0`, `1`, `"yes"`) is DEFER (`EVIDENCE_INVALID`). The determiner never interprets a non-boolean value.

**The request is validated.** The action type must be exactly `RELEASE_WORK_ORDER`, the work order must exist in the MES, and the quantity must be a whole number from 1 to the planned quantity on the record. Booleans, floats and numeric strings are rejected. Any failure is HALT.

The synthetic rules in full:

- **HALT**: invalid request; proposal facts disagree with the MES record; a revoked approval with nothing else in force covering the action; no approval in force covers the record's line, part, revision and quantity; an unrecognized approval status; or a check that is `false`.
- **DEFER**: required evidence is missing or unusable (the supervisor approval, or a check that is absent, `null` or not a boolean).
- **REFER**: the part on the MES record is safety-critical and Quality Engineering hasn't signed off.
- **ADMIT**: none of the above.

When several apply, precedence is **HALT > DEFER > REFER > ADMIT**. All findings are still reported as reason codes.

### Revoked, then reissued: the decision

**Revocation is per approval.** Revoking an approval withdraws that approval only. A different approval from the same role that is active, in force and covers the action is honoured on its own, and the revoked one is reported as `REVOKED_APPROVAL_NOT_RELIED_ON`. The new approval doesn't need to reference the revoked one.

- If a revoked approval exists for the work order and nothing else in force covers the action, the result is **HALT**, not DEFER. The authority withdrew consent; it didn't just fail to give it.
- An approval named in another approval's `supersedes` field is **out of force**, even if it is still marked ACTIVE. An amendment, for example one that narrows the quantity, therefore replaces the original instead of adding to it.
- An approval from a different role never stands in for the required role.
- Any status other than exactly `ACTIVE` or `REVOKED` halts.
- If an approval is revoked **after** an ADMIT but before release, the receipt stops working. The executor sees that the evidence on record has changed (`EVIDENCE_NOT_CURRENT`).

Trade-off: if an authority has two live approvals for one work order and revokes only one, the other still admits. To stop the work order, the approval system has to revoke every approval for it. The alternative, where a revocation blocks the whole work order until an explicit, later-dated reissue, is stricter. It would need approval timestamps and a `supersedes` link, and it would fail review test 5, which expects an unlinked new approval to admit.

### The receipt (`receipts.py`)

Every determination, whatever the verdict, comes with a receipt signed with HMAC-SHA256 (simulated demo key). It carries:

| Field | Binds the receipt to |
|---|---|
| `work_order_id`, `action_digest` | the exact proposed action (SHA-256 of the whole action) |
| `evidence_digest` | the exact evidence the verdict was based on |
| `policy_version`, `policy_digest` | the policy applied: its declared version and a digest of its content |
| `facts_digest` | the MES record facts the verdict relied on (plant, line, part, revision, planned quantity) |
| `issued_at`, `expires_at` | a 5-minute validity window |
| `receipt_id`, `verdict`, `issuer`, `key_id`, `simulated` | identity and result |

Changing any field invalidates the signature.

### Do: the executor (`executor.py`)

The executor receives the proposed action and the receipt, never the determination. It checks, in order:

| # | Group | Check | Refusal cause |
|---|---|---|---|
| 1 | Request | Action type is `RELEASE_WORK_ORDER` | `ACTION_TYPE_INVALID` |
| 2 | | Work order exists in the MES | `WORK_ORDER_UNKNOWN` |
| 3 | | Quantity is a whole number from 1 to the planned quantity (re-read from the MES) | `QUANTITY_INVALID` |
| 4 | Authenticity | A receipt is present | `RECEIPT_MISSING` |
| 5 | | Signature valid, key known | `SIGNATURE_INVALID` |
| 6 | | Not expired (unreadable expiry counts as expired) | `RECEIPT_EXPIRED` |
| 7 | Binding | Issued for this work order | `WORK_ORDER_MISMATCH` |
| 8 | | Issued for this exact action | `ACTION_MISMATCH` |
| 9 | | Issued under the policy in force (version and content) | `POLICY_VERSION_MISMATCH` |
| 10 | Verdict | Verdict is ADMIT | `VERDICT_NOT_ADMIT` |
| 11 | Currency | Evidence on record still matches `evidence_digest` | `EVIDENCE_NOT_CURRENT` |
| 12 | | MES record facts still match `facts_digest` | `WORK_ORDER_CHANGED` |
| 13 | Single use | Not used before | `RECEIPT_ALREADY_USED` |

If any check fails, the result is `REFUSED` with the cause and a short explanation, and the MES is never called. If all pass, the executor marks the receipt **consumed before calling the MES**, so it is spent whether the release succeeds or fails. A retry needs a new determination. A refused presentation does not consume the receipt.

Why this order:
- **Request checks come first** because they don't depend on the receipt. A request that is invalid on its face is refused for that reason, whatever receipt comes with it.
- **Signature and expiry come before everything that reads the receipt.** Until the signature is checked, every field is unverified input; a tampered HALT-turned-ADMIT has to be reported as tampered, not judged on its verdict field. An expired receipt says nothing about the present, whatever its verdict.
- **Binding comes before the verdict**, so an ADMIT is only ever read from a receipt issued for this request under this policy.
- **Currency checks come after the verdict**, so a HALT, DEFER or REFER is reported as such rather than as stale evidence.
- **Replay comes last**, so only an otherwise-valid receipt is reported as already used.

Missing configuration fails closed: an executor with no active policy, or no evidence source, refuses every receipt.

The executor never produces or infers a verdict. Its results carry no verdict field, and it does not import the determiner, the signer or the demo key; it is handed a `ReceiptVerifier`. The orchestrator (`pipeline.py`) passes every receipt to the executor without looking at the verdict, so every case exercises the executor's own gate.

## Tests

- `tests/test_review_gaps.py`: the 11 review regression tests, unmodified. All 11 fail on the original submission and pass now.
- `tests/test_review_fixes.py`: each review fix tested at both layers. Fail-closed check values; facts from the MES record (including a mislabeled safety-critical work order); quantity and action type rejected by the determiner and, separately, by the executor when handed a valid ADMIT receipt; the revocation rule; receipt bindings; policy changes; evidence or MES facts changing after ADMIT; and check order.
- `tests/test_executor_gate.py`: HALT, DEFER and REFER can't release; missing, edited, forged or expired receipts; wrong work order; changed action; replay after success and after failure; ADMIT followed by MES failure. Each refusal test also asserts the work order is unchanged and the MES was never called.
- `tests/test_determiner.py`: verdict rules, precedence, and that every receipt is signed, bound and labeled simulated.
- `tests/test_pipeline.py`: each case end to end, the gate demo and the CLI.
- `tests/test_structure.py`: exactly the four core verdicts; outcomes aren't verdicts; import boundaries; the determiner gets a read-only MES view; `.gitignore` entries.

Every check was also verified by disabling it in a scratch copy and confirming that at least two tests fail.

## Assumptions

- **Verdict wording.** The four names are unchanged. The one-line comments in `verdicts.py` are my reading of the brief; replace them with the canonical definitions if they differ.
- **Precedence** is HALT > DEFER > REFER > ADMIT.
- **Proposal vs record.** A disagreement on plant, line, part or revision is HALT, so a confused agent can't get an ADMIT even for the right work order.
- **Non-boolean check values** are DEFER (unusable evidence), not HALT, because the determiner doesn't guess what `"FAIL"` or `0` was meant to say.
- **Partial releases** are allowed: any whole quantity from 1 up to the planned quantity, within the approval's `max_quantity`.
- **Single use.** A receipt is consumed on the first release attempt, success or failure. A refusal doesn't consume it.
- **Freshness.** A receipt is expired at exactly its expiry time. The time-to-live is 300 seconds (`fixtures/policy.json`).
- **One policy in force.** Receipts issued under any other policy version or content are refused, so a policy change means outstanding receipts need a new determination.

## Limitations and remaining gaps

- **HMAC is symmetric.** The verifier holds the same secret as the signer, so the guarantee that the executor can't create receipts is structural (it only gets a verifier and never imports the signer or key, enforced by tests), not cryptographic. A production system would use asymmetric signatures (for example Ed25519) with managed keys and rotation.
- **Evidence is still supplied to the determiner by the caller.** The executor's check against the evidence registry catches doctored or stale evidence at release time, but only because the registry is a system of record the caller can't write. A real determiner would fetch evidence itself.
- **Per-approval revocation** (see the trade-off above).
- **Check-then-act window.** Between the executor's checks and the MES call, the MES record could change. That's harmless in this single-threaded demo; a real integration would make the release conditional on the expected facts digest.
- **The consumed-receipt ledger is in memory.** A real system needs a durable, atomic store (for example a unique insert on receipt ID) shared by every executor instance.
- No real MES or ERP integration, no authentication, no network calls, and no clock-skew or not-before handling.

## Layout

```
wo_demo/
  verdicts.py           the four core verdicts (unchanged)
  models.py             proposed action, MES work order record, approvals, evidence, policy
  determiner.py         SIMULATED mock determiner, issues receipts
  receipts.py           receipt format and MAC helper
  signing.py            SIMULATED HMAC signer (determiner side only)
  verification.py       signature verifier (what the executor gets)
  demo_keys.py          SIMULATED demo key
  executor.py           the gate, single-use ledger, outcomes and refusal causes
  mes.py                SIMULATED in-memory MES and its read-only reader
  evidence_registry.py  SIMULATED evidence system of record
  pipeline.py           runs a case through the four stages
  gate_demo.py          misuse scenarios for `python -m wo_demo gate`
  cli.py                command line
fixtures/               synthetic policy, MES records and cases
tests/                  pytest suite, including the review regression tests
```
