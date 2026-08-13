# Payment Reconciliation Agent

## Overview

An agent that reconciles a payment processor's settlements against an
internal ledger service, classifies disagreements per a written policy, and
routes every exception through a human approval gate before it's considered
final. Built as a Claude Code Skill (`.claude/skills/payment-reconciliation/`)
driving a plain async Python implementation in `starter/agent_skeleton.py`.

## Business Problem

Finance Operations needs month-end close to run clean against
`data/reconciliation_policy.md`, but the ledger service it depends on
(`starter/ledger_api.py`) is maintained by another team and doesn't always
behave: it times out, returns malformed or reformatted data, and sometimes
just can't find a record. The two failure modes that matter more than any
other classification mistake: a payment that silently disappears from the
report, and a payment marked reconciled that was never actually verified.
Everything in this agent is built to make both of those impossible rather
than just unlikely.

## Features

- Generic response validation against a documented contract — catches
  malformed ledger responses (nested envelopes, null fields, reformatted
  dates/amounts) without special-casing specific order references.
- Retry with reason: each failed attempt records *why* it failed so the
  next attempt (and the eventual escalation writeup) can say what actually
  went wrong.
- Escalation after 3 failed attempts per record, classified by failure
  type (service timeout vs. invalid response), written to `ESCALATION.md`.
- A human approval gate that genuinely halts before any exception is
  finalized — no exception reaches the report without an explicit accept
  or reject.
- A written, per-period reconciliation report — not just console output —
  with every exception grouped by class and every escalation grouped by
  reason.
- Structured logging sufficient to reconstruct any single payment's path
  through the pipeline after the fact.

## Agent Architecture

Plain async Python, not a graph framework — the control flow is linear, so
one `async def` per stage is enough:

```text
fetch_node → validate_response → reconcile_node → approval_node → escalate_node
```

`run_all` loops over every payment, tracking attempts per record (max 3)
to decide retry vs. escalate. Two invariants constrain every stage:

1. Every payment lands in exactly one of four buckets — reconciled,
   exception, awaiting approval, escalated — and the four counts are
   asserted against the input count before a report is written.
2. A record with no trustworthy ledger response is escalated, never
   reconciled. Nothing is ever coerced or guessed to keep a record moving.

## Tech Stack

- Python 3.11+ (developed and tested on 3.14)
- `pydantic` — response validation
- `pytest` / `pytest-asyncio` — test runner (`asyncio_mode = "auto"`)
- `pyright` — strict-mode type checking
- `ruff` — linting

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate
pip install pydantic pytest pytest-asyncio pyright ruff
```

## Usage

Interactively:

```bash
python starter/agent_skeleton.py
```

```text
Reconciliation period (YYYY-MM) [2026-03]:
Reference (today's) date [2026-04-05]:
```

Press Enter on either prompt to accept the shown default. Each flagged
exception is then shown with its evidence and asks for confirmation:

```text
Confirm this exception? (Y/n):
```

Pressing Enter confirms the exception — the safe default, since rejecting
requires a deliberate override.

Programmatically (e.g. for tests or automation), `run_all` takes `period`,
`today`, and an injectable `decide` callback as plain parameters, so a run
never has to touch stdin.

## Testing

```bash
python -m pytest starter/test_agent_skeleton.py -v
python -m pyright
python -m ruff check starter/agent_skeleton.py starter/test_agent_skeleton.py
```

The suite covers the agent loop end-to-end, `fetch_node` mocked per known
misbehaviour class, each recovery path, escalation after exhausted
retries, and the approval gate actually blocking rather than logging and
continuing.

## AI/LLM Evaluation & Observability

This agent doesn't call an LLM at runtime — reconciliation is deterministic
policy logic, so there's no model output to score. In its place:

- **Evaluation** is the `pytest-asyncio` suite: it substitutes for
  LLM-eval by exercising every documented ledger misbehaviour, every
  recovery path, and the escalation path against known-good expected
  outcomes.
- **Observability** is a structured log line at every stage transition
  per payment (`reconciliation.log`), an audit trail of every escalated
  record's attempts (`ESCALATION.md`, rewritten fresh each run), and a
  written, per-period reconciliation report — enough to reconstruct why
  any single payment ended up where it did, without re-running anything.

## Project Structure

```text
starter/
  agent_skeleton.py        agent implementation
  ledger_api.py             ledger service stub (read-only, do not modify)
  test_agent_skeleton.py    pytest-asyncio suite
data/
  processor_payments.csv    input payments
  reconciliation_policy.md  governing policy
docs/specifications/
  payment-reconciliation.md extended task requirements
.claude/skills/payment-reconciliation/
  SKILL.md                  implementation guide for this agent
criteria_checklist.md       submission checklist
RECONCILIATION_REPORT_<period>.md  generated report (per period, per run)
ESCALATION.md               generated escalation log (rewritten each run)
reconciliation.log          generated structured log
REFLECTION.md               submission reflection (hand-authored)
```
