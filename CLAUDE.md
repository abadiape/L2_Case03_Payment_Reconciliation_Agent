# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A payment reconciliation agent exercise. `starter/agent_skeleton.py` is an
unimplemented skeleton (every function raises `NotImplementedError`) that must
be built into a working LangGraph agent. `starter/ledger_api.py` is a stub of
an internal ledger service that deliberately misbehaves in specific,
documented ways — it is the grading fixture, not something to fix.

The task: for each of the 24 payments in `data/processor_payments.csv`, fetch
the matching ledger entry via `ledger_api.fetch_ledger_entry(order_ref)`,
reconcile it against `data/reconciliation_policy.md`, and produce a
reconciliation report where every payment ends up in exactly one bucket:
reconciled, exception, awaiting approval, or escalated. Counts must sum to 24.

## Environment

- Python 3.14, venv already created at `.venv` (`C:\Python314`).
- No `requirements.txt`/`pyproject.toml` exists yet — dependencies
  (`langgraph`, `pydantic`, `deepdiff`, `pytest-asyncio`) are not installed.
  Set up dependency management before writing code.
- No commits exist yet (`git status` shows a fresh, uncommitted repo).

## Architecture

**`starter/ledger_api.py` is read-only / do-not-modify.** It stands in for a
real service maintained by another team; the grader swaps in a version with
*additional* undisclosed misbehaviours and runs the agent unchanged against
it. Any fix that special-cases today's known quirks (rather than validating
the response shape generically) will fail against the grading variant.

Known misbehaviours already present in the stub, per `order_ref`:

| order_ref | Misbehaviour |
|---|---|
| ORD-70003, ORD-70019 | `amount` arrives as a formatted string, not a float |
| ORD-70005 | `currency` arrives as `null` |
| ORD-70009, ORD-70022 | response is a nested envelope (`{"data": {"entry": {...}}}`) instead of a flat object |
| ORD-70016 | `posted_date` arrives in `DD/MM/YYYY` instead of `YYYY-MM-DD` |
| ORD-70017 | `amount` arrives with its sign stripped |
| ORD-70020 | transient — raises `TimeoutError` twice, succeeds on the 3rd call |
| ORD-70023 | persistent — always raises `TimeoutError` |
| any unknown order_ref | returns `{"error": "not_found", ...}` |

Call `ledger_api.reset()` between test cases — the stub tracks per-`order_ref`
call counts internally (`_CALLS`) to drive the transient-failure behaviour, and
state leaks across tests otherwise.

**`starter/agent_skeleton.py`** defines the pipeline to implement, as a
LangGraph graph over `ReconState`:

- `validate_response(raw)` — validate a ledger response against the
  documented contract *before* trusting it (well-formed JSON is not the same
  as a trustworthy shape). Normalize or raise; never coerce a bad value into a
  guess.
- `fetch_node` — calls the ledger service, runs `validate_response`, and on
  failure records the specific reason and increments `attempts`, so retries
  carry the failure reason rather than repeating the identical call blindly.
- `reconcile_node` — applies the policy's matching rules (tolerance, timing,
  currency, duplicates, refunds) and sets `status` + `exception_class` with
  evidence attached.
- `approval_node` — the human validation gate; must genuinely halt (not log
  and continue) before any exception reaches the close file.
- `escalate_node` — after 3 failed attempts on a record, stop retrying and
  write the record, every attempt, what came back each time, and what's
  needed to proceed, to `ESCALATION.md`.
- `build_graph` / `run_all` — wire `fetch -> (retry | reconcile | escalate) ->
  approval` and run all 24 payments, producing the final report.

## The policy (`data/reconciliation_policy.md`)

Reconciliation period is March 2026; treat 2026-04-05 as "today". Key rules
the implementation must encode:

- **Match** processor payment to ledger entry on `order_ref`.
- **Reconciled** requires amount within **0.02** tolerance AND identical
  currency.
- **Timing**: a ledger posting within **3 calendar days** of settlement
  (inclusive of month-boundary crossings) is normal, not an exception.
- **Exception classes**: amount mismatch (beyond tolerance), currency
  mismatch, unmatched payment (no ledger entry), duplicate settlement (more
  than one payment against an order where one is expected).
- Refunds are negative payments matched to a ledger entry with
  `entry_type: "refund"` — they reconcile normally, not as exceptions.
- **No exception is written to the close file without human approval** — this
  applies to every exception class.
- **After 3 failed attempts on the same record, stop and escalate** — never
  loop indefinitely, never substitute a guessed/default value to keep a
  record moving.
- **A record that could not be verified is never marked reconciled.**

## Grading emphasis

Two failure modes are weighted above everything else and cap scoring
regardless of other quality: a payment record that disappears from the report
(the 24 totals must always reconcile), and a record marked `reconciled` that
was never actually verified. Optimize for those two invariants above all
else — when in doubt, escalate rather than guess or drop a record.

Required artifacts from an actual run (not hand-written): `ESCALATION.md` and
a `REFLECTION.md` (600–1000 words, six sections, weighted toward the failure
analysis). See `criteria_checklist.md` for the full deliverables and
self-review checklist before considering this done.
