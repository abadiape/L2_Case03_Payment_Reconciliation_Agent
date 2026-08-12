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

The same reasoning applies to `posted_date` arriving as `DD/MM/YYYY`
(slashes) instead of ISO — it is an equivalent representation of the same
date (this ledger's own GBP-locale convention), not a different fact, so
accept it the same way a numeric-string amount is accepted rather than
rejected. This is *not* the same risk as guessing an ambiguous format
blind: the transform is always day-first, matching both the stub's own
mutation and standard UK/European convention, so commit to that reading
consistently rather than trying both orderings. A day/month that's still
out of range after that reading (e.g. month 25) fails naturally and stays
rejected — never silently coerced into *some* date just to keep going.

**The raised message must state the field, the problem, and the actual
value received — never relay a library's raw internal wording.**
`pydantic.ValidationError.errors()` gives structured `loc`/`msg`/`input` for
exactly this purpose; build the message from those fields
(`f"{loc}: {msg}, received: {input!r}"`), not from `str(exc)`. Concretely:
for ORD-70005 (ledger currency arrives as `null`), the raised reason must
read like `"currency: field is required, received: None"`, not pydantic's
default `"Input should be a valid string"` — the reader needs to know a
field was missing and what actually came back, not just that some string
constraint failed. The same standard applies to `ESCALATION.md` and the
log line, since both relay this message verbatim.

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

Apply `data/reconciliation_policy.md` numerals 1–4 exactly. **Name each
exception class with the policy's own wording, verbatim** (`"Amount
mismatch"`, `"Currency mismatch"`, `"Unmatched payment"`, `"Duplicate
settlement"`) — not a snake_case translation of it. These strings are read
directly by Rina in the report and evidence text, so they should match the
document she already owns rather than requiring her to map a code-ism back
to the policy in her head.

- **Tolerance**: amounts within 0.02 absolute → reconciled on that axis; a
  gap of 0.02 or less is not an exception, and reporting it is a false
  positive.
- **Timing**: a ledger post within 3 calendar days of the settlement date
  is normal, including across a month boundary — do not flag it, and don't
  record it in evidence either. It is not a matching criterion at all, so
  restating "Xd, informational" on every record is noise, not evidence.
- **Currency**: must match exactly.
- **Duplicates**: more than one payment against an `order_ref` where only
  one ledger entry exists is a "Duplicate settlement" exception. **Flag
  only the later payment(s), not every claimant.** Order claimants by
  `settled_date` (earliest first); the earliest is the presumed original
  settlement and reconciles normally like any single-match payment — only
  the later payment(s) become "Duplicate settlement" exceptions, each
  naming the original's `payment_id` in its evidence. Flagging every side
  of the pair means Rina reviews the same anomaly twice for one root cause;
  that is not "more thorough," it is duplicate work for her.
- **Refunds**: a negative payment amount matched to an `entry_type:
  "refund"` ledger entry reconciles like any other record — it is not an
  exception by virtue of being negative.

Attach evidence to every non-reconciled record: the payment's fields and
the ledger entry's fields (raw and normalized) for whichever rule fired —
nothing that doesn't affect the classification belongs in the evidence.
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

**Classify each escalation by failure reason, generically by exception
type — never leave escalated records in one undifferentiated bucket.**
Bucket a persistent `TimeoutError`/`ConnectionError`/`OSError` as "Ledger
Service Timeout"; bucket a persistent shape/validation failure (null field,
bad date format, unrecognised envelope, etc.) as "Ledger Response Invalid".
Do this by exception type, not by `order_ref` — the grader's hidden
variant will introduce failure shapes you haven't seen, and a type-based
rule still classifies them sensibly. A record that never resolves (a
not-found response) is *not* an escalation reason: `LedgerNotFound` is a
clean, well-formed answer per the contract, so it flows straight to
`reconcile_node`'s "Unmatched payment" exception and never reaches this
function at all.

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

## The reconciliation report — a required, standalone artifact

`docs/specifications/payment-reconciliation.md` and
`data/reconciliation_policy.md` #7 both require an actual **report**, not
just a console printout that disappears when the process exits. `run_all`
must write a report file, one per period (e.g. `RECONCILIATION_REPORT_2026-03.md`)
so a later run never overwrites an earlier period's evidence, containing:

- The four totals (reconciled / exceptions / awaiting approval /
  escalated), and confirmation they sum to the input count.
- **Every exception grouped by class** ("Amount mismatch", "Currency
  mismatch", "Unmatched payment", "Duplicate settlement" — the policy's own
  wording, verbatim) with its evidence — this is the "exception classes to
  report" requirement from policy #4, and it is not satisfied by a bare
  count.
- Every record still `awaiting_approval`, with its evidence.
- **Every escalated record grouped by reason** (see `escalate_node`
  above), not just a bucket count.

Console output (`_print_report` or equivalent) is a convenience on top of
this file, not a substitute for it.

## Observability

Emit a structured log line (or equivalent record) at every stage
transition per payment — enough that, given a payment ID or order
reference, you can reconstruct what was attempted, what came back, and why
it ended where it did. This is graded directly. At minimum, log:

- Each `fetch_node` attempt, with its outcome and whether the record was
  simply not found (a fact, not a failure).
- Each retry, carrying the previous attempt's specific failure reason.
- Escalation, with its classified reason.
- `reconcile_node`'s outcome, including the evidence string — not just the
  status and exception class, which alone don't let Rina or a reviewer
  reconstruct *why*.
- The approval gate's actual verdict (`accept` / `reject` / no decision),
  logged distinctly from the record's resulting status, since "the agent
  decided" and "a human decided X" are different facts worth telling apart.

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
