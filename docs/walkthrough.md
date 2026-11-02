# Work order release demo: 5-minute walkthrough

A script for running the demo for a manufacturing audience. Everything shown is simulated, with made-up work orders.

## Before you start

The demo runs in a terminal with nothing to install beyond Python. Set it up once, then rehearse every command before the call.

- [ ] Python 3.10 or later: `python3 --version` (on Windows, use `python` wherever this script says `python3`).
- [ ] Get the code: `git clone https://github.com/adwaitchavann/work-order-release-demo.git`, then `cd work-order-release-demo`.
- [ ] Until PR #1 is merged, use the newest branch: `git checkout read-only-records-atomic-claim`. The `state` command and cases 07 and 08 aren't on `main` yet.
- [ ] Check it works: `python3 -m wo_demo list` prints eight cases.
- [ ] Make the terminal at least 110 characters wide with a large font, and type `clear` between commands.
- [ ] Run all five commands below once before the call, so nothing on screen is a surprise.
- [ ] Keep the one-page explainer open for the opening diagram.

## The script

Seven segments, about 490 spoken words and five commands, which leaves time to point at the screen. Spoken lines are in quotes; the rest is what to do.

### 0:00–0:40 · Why this exists

**Show:** the diagram in the one-page explainer.

**Say:** "AI planning agents are getting good at deciding what to release next. The risk is letting one act on its own say-so. If it's wrong about a revision, a hold or an approval, you find out on the floor. This demo shows one way to let an agent propose releases while your own systems keep the final say. There are four parts: the agent proposes, a gate decides, an executor verifies, and the MES enforces. Everything you'll see is simulated, with made-up work orders."

### 0:40–1:30 · A normal release

**Run:** `python3 -m wo_demo run --case 01_admit_release`

**Say:** "The agent asks to release work order 1001, 400 units. Look at the evidence. The top half comes from the MES and the approval system. The bottom half is what the agent claims, and it's labeled not authoritative. Here they agree, and the supervisor's approval covers the quantity. So the gate says ADMIT and signs a permission slip for this exact order. The executor checks the slip and releases. That's one call to the MES, and the MES records that all 400 were released."

**Point at:** `authoritative` and `caller claims (not authoritative)` under [2]; `verdict: ADMIT` under [3]; `PLANNED -> RELEASED (MES calls: 1)` under [4], and the released-quantity line below it.

### 1:30–2:15 · The agent is wrong

**Run:** `python3 -m wo_demo run --case 07_halt_claim_contradicted_by_mes`

**Say:** "Same agent, same confident claim that everything's ready. But quality has a hold on this order in the MES. The gate goes by the MES, not the agent, so it's a HALT. It also flags that the agent's claim contradicts the record. Nothing is sent to the MES."

**Point at:** `quality_hold_clear=FAIL` in the authoritative line against `quality_hold_clear=pass` in the claims; `verdict: HALT` and `CLAIM_MISMATCH`; `MES calls: 0`.

### 2:15–2:45 · When a person has to decide

**Run:** `python3 -m wo_demo run --case 05_refer_quality_signoff`

**Say:** "This part is safety-critical, and quality engineering hasn't signed off. The gate doesn't guess, and it doesn't just block. It refers the decision to quality. If information were simply missing, like no reading on whether material is staged, it would wait instead. That's a DEFER."

**Point at:** `verdict: REFER (referred to QUALITY_ENGINEERING)`.

### 2:45–3:45 · A hold that lands at the last second

**Run:** `python3 -m wo_demo state`

**Say:** "This is the case plants worry about most. Look at steps 15 to 18. The gate admits the order, and the executor's checks all pass. Then, seconds before the release, quality places a hold. The executor only ever asks the MES to release if nothing has changed since its check, so the MES rejects it. The next proposal for that order is a HALT. Steps 1 to 4 show the simpler version: a slip issued before a hold is refused once the hold is in place."

**Point at:** steps 15–18, especially `FAILED STATE_CHANGED_BEFORE_RELEASE`; then step 4, `REFUSED EVIDENCE_NOT_CURRENT`.

### 3:45–4:30 · The permission slip can't be misused

**Run:** `python3 -m wo_demo gate`

**Say:** "Last, what if the software calling the executor gets it wrong, or someone tampers with a slip? A HALT slip edited to say ADMIT is refused. A slip for one order used on another is refused. A slip used twice is refused, and in our tests two attempts sent at the same moment still give one release. Out of 17 attempts here, only the 3 that passed every check reached the MES."

**Point at:** steps 4, 7 and 14; the last line, `MES release calls: 3`.

### 4:30–5:00 · Close

**Say:** "So the agent proposes, the gate decides, the executor verifies, and the MES enforces. Today the MES and the decision rules are simulated. One gap is still open: an approval withdrawn in those last few seconds isn't caught yet, and that's the next piece of work. For a real plant, the key question is whether your MES can release only if nothing has changed. Which MES are you running, and who signs off a release today?"

## If they ask

**"Is the decision engine real?"** Not in the demo. It's a short set of written rules, labeled as simulated. What the demo shows is the pattern around it: nothing is released on the agent's word, and every release is checked against your records.

**"Would this work with our MES?"** It depends on whether your MES can release an order only if it hasn't changed since it was checked. Some vendors document a way to do that and some don't. We have a short list by vendor ([mes-conditional-writes.md](mes-conditional-writes.md)).

**"Can the agent just call the MES itself?"** Not in the intended architecture: only the executor releases, and it won't act without a valid slip. The demo shows that design, but it runs locally and can't enforce it in a plant. Keeping a real agent away from a real MES takes deployment controls beyond this demo, such as MES credentials and network access that only the executor has.

**"What happens when a release fails?"** The slip is used up either way. A retry needs a fresh decision, which sees the current state. If a hold caused the failure, the retry is a HALT.

**"Can it release part of an order?"** Not in the demo. It releases whole orders only: the gate, the executor and the MES each reject any other quantity, and the MES records what it released. Partial releases would need the MES to track how much of an order has gone out.

**"What if an approval is withdrawn at the last second?"** If it's withdrawn before the executor's final check, it's caught. In the few seconds between that check and the release, it isn't yet. That's the next piece of work.

**"Who sets the rules?"** The plant does. In the demo they sit in one policy file: which approvals are needed, which parts are safety-critical, and which statuses can be released. Every decision records which version of the rules it used.
