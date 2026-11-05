# Work order release demo: 5-minute walkthrough

A script for running the demo for a manufacturing audience. Everything shown is simulated, with made-up work orders.

**Demo version:** commit [`03a8b37905f7c04458b830674d4186d88c557bbe`](https://github.com/adwaitchavann/work-order-release-demo/commit/03a8b37905f7c04458b830674d4186d88c557bbe) on branch `read-only-records-atomic-claim` ([PR #2](https://github.com/adwaitchavann/work-order-release-demo/pull/2)). Everything on screen below comes from that commit.

## Before you start

The demo runs in a terminal with nothing to install beyond Python. Set it up once, then rehearse every command before the call.

- [ ] Python 3.10 or later: `python3 --version` (on Windows, use `python` wherever this script says `python3`).
- [ ] Get the code: `git clone https://github.com/adwaitchavann/work-order-release-demo.git`, then `cd work-order-release-demo`.
- [ ] Check out the reviewed demo version: `git checkout 03a8b37905f7c04458b830674d4186d88c557bbe`. The `state` command and cases 07 and 08 aren't on `main` yet.
- [ ] Check it works: `python3 -m wo_demo list` prints eight cases.
- [ ] Make the terminal at least 110 characters wide with a large font, and type `clear` between commands.
- [ ] Run all five commands below once before the call, so nothing on screen is a surprise.
- [ ] Keep the one-page explainer open for the opening diagram.

## The script

Seven segments, about 530 spoken words and five commands, which leaves time to point at the screen. Spoken lines are in quotes; the rest is what to do. The demo opens with the late quality hold, because it shows the point fastest: an ADMIT is permission to try, and the release can still be refused if the order changes before the MES write.

### 0:00–0:40 · Why this exists

**Show:** the diagram in the one-page explainer.

**Say:** "AI planning agents are getting good at deciding what to release next. The risk is letting one act on its own say-so. If it's wrong about a revision, a hold or an approval, you find out on the floor. This demo shows one way to let an agent propose releases while your own systems keep the final say. There are four parts: the agent proposes, a gate decides, an executor verifies, and the MES enforces. Everything you'll see is simulated, with made-up work orders."

### 0:40–1:40 · A quality hold that lands after the ADMIT

**Run:** `python3 -m wo_demo state`

**Say:** "Let me start with the case plants worry about most. Look at steps 15 to 18. The agent proposes a release, the records agree, and the gate says ADMIT. The executor's checks all pass. Then, before the MES write, quality places a hold on the order. The executor only ever asks the MES to release if nothing has changed since its check, so the MES refuses the release. An ADMIT is permission to try, not a guarantee. The next proposal for that order is a HALT. Steps 1 to 4 show the simpler version: a slip issued before a hold is refused once the hold is in place."

**Point at:** step 15, `ADMIT`; step 16, the hold landing; step 17, `FAILED STATE_CHANGED_BEFORE_RELEASE` (FAILED is how the demo shows the MES refusing the write); step 18, `HALT`; then step 4, `REFUSED EVIDENCE_NOT_CURRENT`.

### 1:40–2:25 · A normal release

**Run:** `python3 -m wo_demo run --case 01_admit_release`

**Say:** "Now the everyday case. The agent asks to release work order 1001, 400 units. Look at the evidence. The top half comes from the MES and the approval system. The bottom half is what the agent claims, and it's labeled not authoritative. Here they agree, and the supervisor's approval covers the quantity. So the gate says ADMIT and signs a permission slip for this exact order. The executor checks the slip and releases. That's one call to the MES, and the MES records that all 400 were released."

**Point at:** `authoritative` and `caller claims (not authoritative)` under [2]; `verdict: ADMIT` under [3]; `PLANNED -> RELEASED (MES calls: 1)` under [4], and the released-quantity line below it.

### 2:25–3:05 · The agent is wrong

**Run:** `python3 -m wo_demo run --case 07_halt_claim_contradicted_by_mes`

**Say:** "Same agent, same confident claim that everything's ready. But quality has a hold on this order in the MES. The gate goes by the MES, not the agent, so it's a HALT. It also flags that the agent's claim contradicts the record. Nothing is sent to the MES."

**Point at:** `quality_hold_clear=FAIL` in the authoritative line against `quality_hold_clear=pass` in the claims; `verdict: HALT` and `CLAIM_MISMATCH`; `MES calls: 0`.

### 3:05–3:35 · When a person has to decide

**Run:** `python3 -m wo_demo run --case 05_refer_quality_signoff`

**Say:** "This part is safety-critical, and quality engineering hasn't signed off. The gate doesn't guess, and it doesn't just block. It refers the decision to quality. If information were simply missing, like no reading on whether material is staged, it would wait instead. That's a DEFER."

**Point at:** `verdict: REFER (referred to QUALITY_ENGINEERING)`.

### 3:35–4:20 · The permission slip can't be misused

**Run:** `python3 -m wo_demo gate`

**Say:** "Last, what if the software calling the executor gets it wrong, or someone tampers with a slip? A HALT slip edited to say ADMIT is refused. A slip for one order used on another is refused. A slip used twice is refused, and in our tests two attempts sent at the same moment still give one release. Out of 17 attempts here, only the 3 that passed every check reached the MES."

**Point at:** steps 4, 7 and 14; the last line, `MES release calls: 3`.

### 4:20–5:00 · Close

**Say:** "So the agent proposes, the gate decides, the executor verifies, and the MES enforces. Today the MES and the decision rules are simulated. One gap is still open, and we treat it as a production blocker: an approval withdrawn between the executor's final approval check and the MES write isn't caught yet. For a real plant, the key question is whether your MES can release only if nothing has changed. Which MES are you running, and who signs off a release today?"

## If they ask

**"Is the decision engine real?"** Not in the demo. It's a short set of written rules, labeled as simulated. What the demo shows is the pattern around it: nothing is released on the agent's word, and every release is checked against your records.

**"Would this work with our MES?"** It depends on whether your MES can release an order only if it hasn't changed since it was checked. That's one of the first things we'd confirm with your MES vendor. *(Presenter note: the MES vendor matrix is for internal discovery prep. Don't share it with prospects.)*

**"Can the agent just call the MES itself?"** Not in the intended architecture: only the executor releases, and it won't act without a valid slip. The demo shows that design, but it runs locally and can't enforce it in a plant. Keeping a real agent away from a real MES takes deployment controls beyond this demo, such as MES credentials and network access that only the executor has.

**"What happens when a release fails?"** The slip is used up either way. A retry needs a fresh decision, which sees the current state. If a hold caused the failure, the retry is a HALT.

**"Can it release part of an order?"** Not in the demo. It releases whole orders only: the gate, the executor and the MES each reject any other quantity, and the MES records what it released. Partial releases would need the MES to track how much of an order has gone out.

**"What if an approval is withdrawn after the final check?"** If it's withdrawn before the executor's final approval check, it's caught. If it's withdrawn between the executor's final approval check and the MES write, it isn't caught yet. We track that as a production blocker, so it has to be closed before this goes near a real plant.

**"Who sets the rules?"** The plant does. In the demo they sit in one policy file: which approvals are needed, which parts are safety-critical, and which statuses can be released. Every decision records which version of the rules it used.
