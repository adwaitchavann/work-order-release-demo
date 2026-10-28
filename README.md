# Work order release demo (SIMULATED)

> **Everything here is simulated.** Determinations come from a mock determiner, receipts are signed with a demo HMAC key, and the MES and the approval system are local in-memory stand-ins loaded from synthetic fixtures. Nothing connects to a real MES. No customer data is used.

An AI planning agent proposes releasing a manufacturing work order. Each proposal goes through four stages:

**proposed action → evidence → determination → effect**

Deciding and doing are separate. The mock determiner decides and issues a signed receipt. A separate executor, the only component that can release a work order, verifies that receipt itself before acting. **ADMIT permits the executor to attempt the release; it does not mean the release succeeded.** The executor reports its own outcome: `RELEASED`, `FAILED` or `REFUSED`.

**The caller is not authoritative for evidence.** The determination reads the work order's facts, status and readiness from the MES, and its approvals from the approval system. Anything the caller says about those is a claim. Caller claims cannot establish admissibility; contradictions may independently cause HALT. See [docs/authoritative-state.md](docs/authoritative-state.md).

**The release is conditional.** The MES applies a release only if the work order is still at the version the executor verified and has no active quality hold, checked and written atomically. A QA hold that lands between the executor's last check and the MES write makes the release fail.

**Reads are read-only.** Records handed out by the MES are immutable copies, so editing one (its readiness signals included) can't change the MES or let a release through.

**A receipt is claimed atomically.** Marking a receipt used is one check-and-set, so of any number of concurrent requests carrying the same receipt, exactly one reaches the MES.

Verdict precedence is **HALT > REFER > DEFER > ADMIT**.

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
python -m wo_demo run                                  # all eight cases, four-stage trace
python -m wo_demo run --case 07_halt_claim_contradicted_by_mes
python -m wo_demo run --json out/trace.json            # also write a JSON trace
python -m wo_demo state                                # authoritative state changing between proposals
python -m wo_demo gate                                 # the executor refusing misuse (17 steps)
python -m wo_demo list                                 # list the cases
python -m pytest -v                                    # 277 tests, plus 1 expected failure (see below)
python -m pytest tests/test_review_gaps.py -v          # the 11 review regression tests
python -m pytest tests/test_authoritative_state.py -v  # the authoritative-state scenarios
python -m pytest tests/test_conditional_release.py -v  # the MES race and atomicity tests
python -m pytest tests/test_read_only_records.py -v    # records edited through the reader can't change the MES
python -m pytest tests/test_atomic_claim.py -v         # concurrent requests with one receipt release once
```

## Fixtures

| File | Plays the part of | Authoritative for |
|---|---|---|
| `fixtures/work_orders.json` | the MES | work order facts, status, readiness signals |
| `fixtures/approvals.json` | the approval system | supervisor and quality engineering approvals |
| `fixtures/policy.json` | the release policy | the rules (version `SYN-REL-POLICY-v4`) |
| `fixtures/cases/*.json` | one proposal each | the requested action only; `caller_evidence` is a claim |

## Cases

| Case | Situation | Verdict | Effect |
|---|---|---|---|
| `01_admit_release` | Caller says releasable; MES and approval system agree | ADMIT | Released |
| `02_admit_execution_failure` | Same, but the MES has the work order locked | ADMIT | Failed (`WORK_ORDER_LOCKED`); receipt spent |
| `03_halt_revoked_approval` | The only supervisor approval was revoked | HALT | Refused |
| `04_defer_missing_evidence` | The MES has no quality-hold reading | DEFER | Refused |
| `05_refer_quality_signoff` | Safety-critical part; no Quality Engineering sign-off | REFER | Refused |
| `06_admit_reissued_approval` | Approval revoked, then a new approval issued | ADMIT | Released |
| `07_halt_claim_contradicted_by_mes` | Caller says releasable; the MES has a quality hold | HALT | Refused |
| `08_defer_mes_state_missing` | Caller says material is staged; the MES has no reading | DEFER | Refused |

`python -m wo_demo state` covers the remaining scenarios: the same proposal decided again after the MES changes, an earlier receipt presented after that change (including after the state changes back), and a QA hold landing between the executor's last check and the MES write (steps 15–18).

## How it works

### Where each input comes from (`authority.py`)

| Input | Authoritative source | The caller's version |
|---|---|---|
| Action type, work order, quantity | the caller (what it is asking for) | validated against the MES |
| Part, revision, line, plant, planned quantity | MES record | disagreement is HALT (`FACT_MISMATCH`) |
| Status | MES | contradiction is HALT; can't fill a gap |
| Readiness: BOM confirmed, material staged, quality hold clear | MES | contradiction is HALT; can't fill a gap |
| Approvals | approval system | contradiction is HALT; approvals the system doesn't have are ignored |
| Rules | policy file | — |

The determiner and the executor both read through `AuthoritativeSources`, a read-only view over the MES and the approval system. Each call returns an `AuthoritativeView` built from two sequential reads, the MES first and then the approval system: the MES record, the status and readiness signals, the approvals, and a version number for each source. **This is not a consistent cross-system snapshot.** The two halves can describe slightly different moments. Any change after a source was read moves that source's version, so the executor's re-read still catches it, but the view as a whole isn't guaranteed to have existed at one instant. The MES version increases on any change to facts, status or readiness, and the approval system's version increases on any change to a work order's approvals. The session lock is execution state and does not change the version.

**Records are read-only.** The only way to change a work order is one of the MES's change methods, and each of those moves the version. A `frozen` dataclass stops fields being reassigned but not a dict inside one being edited, so a record's readiness is stored as a read-only mapping (`MappingProxyType`) over a private deep copy, made whenever a record is built. On top of that, the MES copies every record it is given and returns a fresh copy from every read (`lookup`, `snapshot` and the reader), so even a forced edit to a returned record stays in that copy. `tests/test_read_only_records.py` edits a held work order's readiness through the reader and asserts the determination is still HALT, the release is refused, and the MES record and version are unchanged. Approvals were already immutable (frozen records in a tuple).

### How the caller's claims are treated

- **Caller claims cannot establish admissibility; contradictions may independently cause HALT.** Whether a release is admissible is computed from the authoritative view.
- **Contradicted claims halt.** If the caller's status, readiness value or copy of an approval differs from what the source holds, the result is HALT (`CLAIM_MISMATCH`), the same rule the proposal's facts already follow (`FACT_MISMATCH`). Values are compared strictly, so `1` doesn't match `true`. This applies in both directions: a caller that thinks material isn't staged when the MES says it is doesn't get ADMIT either.
- **Unconfirmed claims are ignored.** A claim about something the source has no value for (no reading, no such approval) is reported as `CLAIM_NOT_USED` and has no effect. It can't fill in missing state.
- **Not bound.** The receipt binds the authoritative view, not the claims.

### Decide: the mock determiner (`determiner.py`)

The verdicts are the four core verdicts, unchanged (`verdicts.py`). The rules are evaluated on the authoritative view:

- **HALT**: invalid request (action type, unknown work order, quantity not a whole number from 1 to the planned quantity); proposal facts or caller claims that contradict the record; a status the policy doesn't list as releasable; a revoked approval with nothing else in force covering the action; no approval in force covers the record's line, part, revision and quantity; an unrecognized approval status; a readiness check that is `false`.
- **REFER**: the part on the MES record is safety-critical and Quality Engineering hasn't signed off in the approval system.
- **DEFER**: required state is missing or unusable: no status reported, no supervisor approval in the approval system, or a readiness check that is absent, `null` or not a boolean.
- **ADMIT**: none of the above.

When several apply, precedence is **HALT > REFER > DEFER > ADMIT**. All findings are still reported as reason codes, most severe first, so a referral that also has missing state lists the missing state too. An unknown work order is HALT, not DEFER: the target doesn't exist, which is different from state being missing.

### Revoked, then reissued

**Revocation is per approval.** Revoking an approval withdraws that approval only. A different active approval from the same role that is in force and covers the action is honoured on its own. If a revoked approval exists and nothing else covers the action, the result is HALT rather than DEFER. An approval named in another's `supersedes` field is out of force, so an amendment replaces the original. Any status other than exactly `ACTIVE` or `REVOKED` halts.

Trade-off: if an authority has two live approvals for one work order and revokes only one, the other still admits.

### The receipt (`receipts.py`)

Unchanged in format and signing. Every determination comes with a receipt signed with HMAC-SHA256 (simulated demo key) that binds the action digest, the work order, an `evidence_digest`, the policy version and content digest, the MES facts digest, and a 5-minute validity window. `evidence_digest` is now the digest of the authoritative view the determination read, including both versions.

### Do: the executor (`executor.py`)

The executor receives the action and the receipt, never the determination, and checks in order:

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
| 11 | Single use | Not already in the ledger (early report; the atomic claim below is the guarantee) | `RECEIPT_ALREADY_USED` |
| 12 | Currency | MES record facts still match | `WORK_ORDER_CHANGED` |
| 13 | | Authoritative view (re-read now) still matches `evidence_digest` | `EVIDENCE_NOT_CURRENT` |

If any check fails, the result is `REFUSED` with the cause, and the MES is never called. If all pass, the executor **claims the receipt right before the MES call**: `ConsumedReceipts.claim()` checks and marks it used in one atomic step and succeeds for exactly one caller. A request that loses the claim is refused with `RECEIPT_ALREADY_USED` and never reaches the MES. Check 11 alone isn't enough, because two concurrent requests can both pass it; the claim is what guarantees one release. The receipt is spent whether the release then succeeds or fails, and a refusal at checks 1–13 doesn't spend it.

`tests/test_atomic_claim.py` holds 2 and then 8 concurrent requests with the same receipt at check 13 until all of them have passed check 11, then lets them go: exactly one is released, the rest are refused `RECEIPT_ALREADY_USED`, and the MES sees one call. With other timings a duplicate is still refused, only with a different cause: one that passes check 11 before the winner claims, but reaches check 13 after the winner's release has committed, is refused `EVIDENCE_NOT_CURRENT`, because the release changed the work order. That interleaving is tested too.

Single use comes before the currency checks because a successful release itself changes the work order's status; a replayed receipt should be reported as already used. Signature and expiry come before anything that reads the receipt; binding comes before the verdict; request checks come first because they don't need the receipt at all.

The executor never produces or infers a verdict. It does not import the determiner, the signer or the demo key. Missing configuration (no active policy, no authoritative source) refuses every receipt.

### The MES write is conditional (`mes.py`)

The executor's last check is a read; the release is a separate write. To stop a release going ahead on earlier state, the executor passes the MES version it verified in check 13, and the MES accepts the release only if, inside one critical section that every other MES change also takes:

| MES check (atomic with the write) | MES error, reported as FAILED |
|---|---|
| The release names an expected version | `PRECONDITION_REQUIRED` |
| The work order is still at that version | `STATE_CHANGED_BEFORE_RELEASE` |
| `quality_hold_clear` is exactly true (an unconfirmed hold counts as active) | `QUALITY_HOLD_ACTIVE` |
| Not locked by another MES session | `WORK_ORDER_LOCKED` |
| Status is still `PLANNED` | `INVALID_STATUS` |

A QA hold placed after the executor's check makes the release fail with `STATE_CHANGED_BEFORE_RELEASE`. A hold that lands without moving the version (a version-tracking bug) is still stopped by the hold check. A hold that arrives while the MES is between its checks and its write waits for the write, and then lands on a released order. The receipt is spent, so a retry needs a new determination, which would HALT on the hold.

**Unresolved: the approval-revocation window.** Approvals live in a separate system the MES can't see. An approval revoked after the executor's last check but before the MES write is not caught; the release goes ahead. `test_approval_revoked_in_the_same_window_is_not_yet_caught` records this as a strict expected failure. Leases and cross-system approval coordination are out of scope for this iteration.

## Tests

- `tests/test_review_gaps.py`: the 11 review regression tests, unmodified.
- `tests/test_authoritative_state.py`: the five authoritative-state scenarios, including six ways the authority can contradict a "releasable" claim, five kinds of missing state, precedence over missing state, state changing between proposals, and earlier receipts refused after changes, including change-and-revert.
- `tests/test_conditional_release.py`: the race (a hold placed between the executor's check and the MES write), other changes in the same window, the hold check without a version bump, unconditional and stale releases, a concurrent hold during the MES's own check-and-write, and the approval-revocation window as a strict expected failure.
- `tests/test_read_only_records.py`: editing a record through the reader (and forcing an edit onto a returned record) can't clear a hold or let a release through; reads return fresh copies, nested values included; mappings handed to the MES are copied.
- `tests/test_atomic_claim.py`: concurrent requests with one receipt release exactly once, including when the one attempt fails; the claim under contention; a refusal before the claim leaves the receipt unspent.
- `tests/test_review_fixes.py`: the earlier review fixes, tested against authoritative state.
- `tests/test_executor_gate.py`, `tests/test_determiner.py`, `tests/test_pipeline.py`, `tests/test_structure.py`: the gate, the rules, the end-to-end runs and demos, and the import and read-only boundaries.

Every check was also verified by disabling it in a scratch copy and confirming tests fail.

## Assumptions

- **Verdict wording.** The four names are unchanged. The one-line comments in `verdicts.py` are my reading of the brief.
- **Precedence** is HALT > REFER > DEFER > ADMIT.
- **Contradicting claims halt** (extends `FACT_MISMATCH` to status, readiness and approvals). Caller claims cannot establish admissibility. Ignoring claims outright would make review test 1 admit.
- **Releasable status** is `PLANNED` (policy). Missing status is DEFER; any other status is HALT.
- **Unknown work order** is HALT, not DEFER.
- **Non-boolean readiness values** from the MES are DEFER (unusable), not HALT.
- **Partial releases** are allowed: any whole quantity from 1 up to the planned quantity, within the approval's `max_quantity`.
- **Single use.** A receipt is spent by the first request that passes every check and wins the claim, whether the release then succeeds or fails.
- **Freshness.** A receipt is expired at exactly its expiry time; TTL is 300 seconds.

## Limitations and remaining gaps

- **Approval-revocation window (unresolved).** The conditional release covers MES state only. An approval revoked between the executor's last check and the MES write isn't caught. Leases and cross-system coordination are out of scope for this iteration.
- **No consistent cross-system snapshot.** The authoritative view comes from sequential reads of the MES and the approval system.
- **HMAC is symmetric.** The guarantee that the executor can't create receipts is structural, not cryptographic. Asymmetric signing is planned separately.
- **The consumed-receipt ledger is in memory.** The claim is atomic within one process (a lock). Production needs the claim as a conditional write in a durable, shared store, so it holds across processes and restarts; planned separately.
- **Source outages aren't modeled.** If the MES or the approval system can't be reached, the result should be DEFER (and a refusal at the executor). Easy to add.
- **Per-approval revocation** (see the trade-off above).
- No real MES or ERP integration, no authentication, no network calls, and no clock-skew or not-before handling.

## Layout

```
wo_demo/
  verdicts.py           the four core verdicts (unchanged)
  models.py             proposed action, MES work order record, approvals, evidence, policy
  authority.py          read-only view over the systems of record, shared by determiner and executor
  mes.py                SIMULATED MES: facts, status, readiness, version; read-only copies; conditional atomic release
  approval_registry.py  SIMULATED approval system: approvals, version; read-only reader
  determiner.py         SIMULATED mock determiner, issues receipts
  receipts.py           receipt format and MAC helper
  signing.py            SIMULATED HMAC signer (determiner side only)
  verification.py       signature verifier (what the executor gets)
  demo_keys.py          SIMULATED demo key
  executor.py           the gate, single-use ledger with atomic claim, outcomes and refusal causes
  pipeline.py           runs a case through the four stages
  state_demo.py         authoritative state changing between proposals (`python -m wo_demo state`)
  gate_demo.py          misuse scenarios (`python -m wo_demo gate`)
  cli.py                command line
docs/
  authoritative-state.md  data flow and who is authoritative for what
fixtures/               synthetic MES, approval system, policy and cases
tests/                  pytest suite, including the review regression tests
```
