# Authoritative state for release determinations

The determiner no longer trusts evidence from the proposing caller. It reads the work order's state from the MES and its approvals from the approval system (both local and simulated). The executor re-reads the same sources before it releases, and the MES applies the release only if the work order is still in the state the executor checked. The receipt format and signing are unchanged.

Verdict precedence is **HALT > REFER > DEFER > ADMIT**.

## Data flow

```
  AI agent (caller)
     |  proposal: the action it wants, plus its own claims about status, readiness and approvals
     v
  Determiner ------------ signed receipt -----------> Executor ---- conditional release (version N) ----> MES
     ^  reads MES, then approvals                        ^  re-reads before releasing                      |
     |                                                   |                                                 |
     +------------- AuthoritativeSources (read-only) ----+       version N, no active hold, not locked,    |
                      ^                         ^                PLANNED: checked and written atomically --+
                      |                         |
          MES: facts, status,          Approval system:
          readiness, version           approvals, version
```

1. The caller proposes an action and may attach claims about status, readiness and approvals.
2. The determiner builds one view from `AuthoritativeSources` and decides from it. The caller's claims are compared against that view.
3. The receipt's existing `evidence_digest` field binds a digest of the view, including both versions. The MES facts digest is bound as before.
4. The executor re-reads the same sources and refuses if anything the determination relied on has changed.
5. The executor claims the receipt in one atomic check-and-set, so only one request carrying it can go on.
6. The executor then asks the MES to release *on condition* that the work order is still at the MES version it just verified. The MES checks that version, checks for an active quality hold, and writes, all in one critical section.

**Consistency of the view.** The view comes from two sequential reads, the MES first and then the approval system. It is not a consistent cross-system snapshot: the two halves can describe slightly different moments. Any change after a source was read moves that source's version, so the executor's re-read still catches it. What isn't guaranteed is that the view as a whole existed at one instant.

**Reads can't change the source.** Records the MES hands out are immutable copies. Readiness is a read-only mapping over a private deep copy, and every read (including through the read-only reader) returns a fresh copy, so editing a record a reader holds changes neither the MES nor its version. Before this, a reader could edit the readiness dict inside a record, clear a hold without the version moving, and the release went through. `tests/test_read_only_records.py` is the regression test.

## Who is authoritative for each input

| Input | Authoritative source | What the caller's version can do |
|---|---|---|
| Action type, work order, quantity | The caller: it's what it is asking for | Validated against the MES |
| Part, revision, line, plant, planned quantity | MES record | Disagreement is HALT (`FACT_MISMATCH`, unchanged) |
| Work order status | MES | Contradiction is HALT; can't stand in if missing |
| Readiness: BOM confirmed, material staged, quality hold clear | MES | Contradiction is HALT; can't stand in if missing |
| Supervisor and Quality Engineering approvals | Approval system | Contradiction is HALT; approvals the system doesn't have are ignored |
| Rules: required checks, safety-critical parts, releasable statuses, TTL | Policy file | Not applicable |

## How the caller's claims are treated

- Caller claims cannot establish admissibility; contradictions may independently cause HALT.
- A claim that contradicts a value the source does have is HALT (`CLAIM_MISMATCH`), compared strictly (`1` isn't `true`).
- A claim about something the source doesn't have (no reading, no such approval) is ignored and noted as `CLAIM_NOT_USED`. It can't fill a gap.
- Claims are not bound into the receipt.

## The five scenarios

| Scenario | Result | Shown in |
|---|---|---|
| 1. Caller says releasable, authority agrees | ADMIT, released | Case 01; `test_agreeing_claim_gets_the_expected_admit_and_releases` |
| 2. Caller says releasable, authority disagrees | HALT, never ADMIT | Case 07; six variants (quality hold, material, on hold, already released, approval revoked, approval narrowed) |
| 3. Required authoritative state missing | DEFER, unless HALT or REFER also applies | Case 08; five kinds of missing state; precedence tests |
| 4. State changes between two proposals | The second determination follows the new state | `python -m wo_demo state`, steps 1–3 and 10–13 |
| 5. Earlier receipt after the state changed | REFUSED `EVIDENCE_NOT_CURRENT`, even if the state changes back | `state` steps 4 and 7; 11 tests |

## Conditional release at the MES write

The executor's last check is a read, and the release is a separate write. A QA hold placed between them would otherwise be missed. So the MES now only accepts a conditional release:

- **Version check.** The executor passes the MES version it verified. If the work order has changed since then (a hold, a status change, a readiness or facts change), the MES rejects the release with `STATE_CHANGED_BEFORE_RELEASE`.
- **Active-hold check.** Independently, the MES refuses any release while `quality_hold_clear` isn't exactly true (`QUALITY_HOLD_ACTIVE`). That covers the dangerous case even if version tracking were wrong.
- **Atomic.** Those checks, the session-lock and status checks, and the write all happen inside one critical section that every other MES change also has to take. A hold that arrives during the release waits until the release has committed, and then lands on a released order.
- **Outcome.** The executor reports FAILED with the MES error code. The receipt is already spent, so a retry needs a new determination, which would now HALT on the hold.
- **Tests.** `tests/test_conditional_release.py` places a hold exactly in the window (between the executor's check and the MES write) and asserts no release, `STATE_CHANGED_BEFORE_RELEASE` and FAILED. It also covers a hold that lands without a version bump, and a hold attempted from another thread during the MES's own check-and-write. The race is also step 16–17 of `python -m wo_demo state`.

## Single-use claim

The executor's "not used" check (11) and marking the receipt used used to be separate steps, so two concurrent requests with the same receipt could both pass the check and both call the MES. Now `ConsumedReceipts.claim()` does the check and the mark in one atomic step, right before the MES call, and only one caller can win it. Check 11 stays as an early report. The losers are refused `RECEIPT_ALREADY_USED` and never reach the MES. `tests/test_atomic_claim.py` holds 2 and 8 concurrent requests until all have passed check 11 and asserts exactly one release and one MES call. The ledger is still in memory, so the claim is atomic within one process. A durable claim shared across processes is part of the separate receipt work.

## Decisions to confirm

1. **Contradicting claims halt.** I extended the existing `FACT_MISMATCH` rule to status, readiness and approvals. The alternative is to ignore caller evidence completely, but then review test 1 would fail: it still sends `"FAIL"`, `0` and so on through the caller and expects no ADMIT. If you'd rather ignore claims outright, test 1 should set the MES value instead.
2. **New rule: the MES status must be releasable** (`PLANNED` in the policy). A missing status is DEFER; anything else is HALT. The policy version is bumped to v4.
3. **An unknown work order stays HALT**, not DEFER. The target doesn't exist, which is different from state being missing.
4. **Versions.** The MES and the approval system each keep a per-work-order version that's included in the evidence digest, so a change that's later reverted still invalidates an earlier receipt. The session lock doesn't change the version.
5. **Executor check order.** Single use comes before the currency checks, and the facts check before the evidence check. This only changes which cause is reported when several apply.

## Unresolved, out of scope for this iteration

- **Approval-revocation window.** Approvals live in a separate system, and the MES conditional write can't see them. An approval revoked after the executor's last check but before the MES write is not caught, and the release goes ahead. This is recorded as a strict expected failure (`test_approval_revoked_in_the_same_window_is_not_yet_caught`), so it shows up as soon as anyone closes it. Leases and cross-system approval coordination are deliberately left out of this iteration.
- **No cross-system snapshot.** See "Consistency of the view" above.
- **Receipt format and HMAC signing are unchanged.** The single-use claim is now atomic within the process. Asymmetric signing and a durable, cross-process ledger are being handled separately.
- **Source outages aren't modeled.** If the MES or the approval system can't be reached, the result should be DEFER.
