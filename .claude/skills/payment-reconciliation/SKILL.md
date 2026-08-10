---
name: payment-reconciliation
description: Implements, extends, and debugs the payment reconciliation agent in starter/agent_skeleton.py — matches processor payments to ledger entries, validates ledger responses defensively, classifies exceptions per policy, retries with reasons, escalates after 3 failed attempts, and gates exceptions behind human approval. Use when asked to build or fix the reconciliation agent, validate ledger_api responses, generate ESCALATION.md/REFLECTION.md, or write its pytest-asyncio suite.
---

## Objective

Fill in `starter/agent_skeleton.py` so it reconciles every payment in
`data/processor_payments.csv` against `starter/ledger_api.py`, per the rules in
`data/reconciliation_policy.md` and the task framing in
`docs/specifications/payment-reconciliation.md`. Read both before writing
code — this file tells you how to structure the work, not what the rules are.

## Non-negotiables

Two outcomes cap grading regardless of everything else:

1. **A record disappears from the report.** Every payment must land in
   exactly one bucket: reconciled, exception, awaiting approval, or
   escalated. Assert the four counts sum to the input row count before
   `run_all` returns — don't just hope the loop covered everyone.
2. **A record is marked reconciled without being verified.** If
   `fetch_node` never got a trustworthy response for a payment, that record
   is escalated, never reconciled. Never substitute a guessed or coerced
   value to keep a record moving.

When unsure which bucket a record belongs in, escalate — reconciling twenty
records and clearly escalating four beats reconciling all twenty-four with
some of them wrong.

## Architecture: plain async Python, not LangGraph

The starter's docstring suggests LangGraph. Don't use it here — the control
flow is linear (fetch → validate → classify → approve, with a bounded
3-attempt retry) and graph machinery adds dependency weight and boilerplate
without buying anything a plain `async def` per stage doesn't already give
you. Skip `deepdiff` too; a couple of tolerance/equality checks don't need
it. Keep `pydantic` (response validation) and `pytest-asyncio` (required test
runner).

Implement each stage already named in `agent_skeleton.py`
(`validate_response`, `fetch_node`, `reconcile_node`, `approval_node`,
`escalate_node`) as its own small async function operating on `ReconState`.
`run_all` is a loop over payments that calls them in sequence, tracking
`attempts` per record to decide retry vs. escalate. No `build_graph()` — if
you want to keep the name for continuity, have it just return the plain
callables in a dict; don't add a graph library to justify it.

## `validate_response`

Define a pydantic model for the documented contract exactly:
`entry_id: str, order_ref: str, amount: float, currency: str,
posted_date: date, entry_type: str`. Parse the raw dict against it and raise
on anything that doesn't fit — a nested envelope, a null field, a wrong
type. This is what catches misbehaviours generically instead of special-casing
`order_ref` values, which matters because the grader runs an unseen variant
of the stub with *additional* misbehaviours.

Decide once, as a stated rule, whether a numeric string amount
(`"1240.00"`) is an acceptable coercion or a shape violation — either is
defensible, but apply it uniformly rather than allowing it for some
records and not others.

## `fetch_node`

Call `ledger_api.fetch_ledger_entry(order_ref)`, run the result through
`validate_response`, and catch both the validation error and any exception
the stub raises (e.g. `TimeoutError`). On failure, record the specific
reason and increment `attempts` — don't just retry the identical call and
hope; the retry needs the failure reason attached so the next attempt (or
the escalation writeup) can say what actually went wrong. Keep this
function a single attempt — looping and sleeping belong in `run_all`, not
here, so this stays unit-testable with a mocked `fetch_ledger_entry`.

## `reconcile_node`

Apply `data/reconciliation_policy.md` numerals 1–4 exactly:

- **Tolerance**: amounts within 0.02 absolute → reconciled on that axis; a
  gap of 0.02 or less is not an exception, and reporting it is a false
  positive.
- **Timing**: a ledger post within 3 calendar days of the settlement date
  is normal, including across a month boundary — do not flag it.
- **Currency**: must match exactly.
- **Duplicates**: more than one payment against an `order_ref` where only
  one ledger entry exists is a duplicate-settlement exception.
- **Refunds**: a negative payment amount matched to an `entry_type:
  "refund"` ledger entry reconciles like any other record — it is not an
  exception by virtue of being negative.

Attach evidence to every non-reconciled record: the payment's fields, the
ledger entry's fields (raw and normalized), and which specific rule fired.
Rina approves or rejects from that evidence alone — she shouldn't have to
re-derive your reasoning.

## `approval_node`

Must genuinely halt before any exception is written to the close file — a
warning that logs and continues does not satisfy this. Show the evidence
attached in `reconcile_node` at the point of the halt.

## `escalate_node`

Triggered after the 3rd failed `fetch_node` attempt for a record — stop
retrying at that point, don't loop further, and don't guess a value to keep
the run going. Append one block per escalated record to `ESCALATION.md`:
the record's identity, every attempt with what was sent and what came back,
and what would be needed to proceed. `ESCALATION.md` must come from an
actual run, not be hand-written.

## `run_all` and the period/today input

`docs/specifications/payment-reconciliation.md` asks for the reconciliation
period and a "today" reference date as input, rather than hardcoding March
2026. Implement this TTY-aware so grading can't hang on stdin:

- If `sys.stdin.isatty()`, prompt interactively — show
  `Reference (today's) date [<real today>]:` with the actual current date
  as the visible default in parentheses, and accept blank input to take
  that default. Same idea for the period.
- If not a TTY (pytest, the grader), skip the prompt entirely and use the
  real current date plus a period inferred from the input data — never
  block waiting for input that won't come.
- `run_all(period=None, today=None)` should accept these as plain optional
  parameters underneath the prompting, so tests call it directly without
  touching stdin at all.

Before returning the report, assert reconciled + exceptions + awaiting
approval + escalated == number of input payments.

## Observability

Emit a structured log line (or equivalent record) at every stage
transition per payment — enough that, given a payment ID or order
reference, you can reconstruct what was attempted, what came back, and why
it ended where it did. This is graded directly.

## Testing

Write the pytest-asyncio suite required by the checklist, covering:

- The agent loop end-to-end across a small fixture set.
- `fetch_node` with `ledger_api.fetch_ledger_entry` mocked per misbehaviour
  class: nested envelope, null field, string-formatted amount, sign-stripped
  amount, transient timeout (succeeds within retry budget), persistent
  timeout (exhausts retries), not-found.
- Each recovery path (retry with reason) and the escalation path
  (`ESCALATION.md` content after 3 failures).
- The approval gate actually blocking, not just returning a value.

Call `ledger_api.reset()` between test cases — the stub tracks per-`order_ref`
call counts to drive its transient-failure behaviour, and that state leaks
across tests otherwise.

## Deliverables

See `criteria_checklist.md` for the full submission checklist,
self-review questions, and the `REFLECTION.md` requirement (600–1000 words,
six sections, weighted toward the failure sections). Don't duplicate its
content here — read it directly before considering the work done.
