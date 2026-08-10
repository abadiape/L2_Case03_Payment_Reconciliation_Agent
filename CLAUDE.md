# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A payment reconciliation agent. `starter/agent_skeleton.py` implements the
pipeline as **plain async Python** — one `async def` per stage
(`validate_response`, `fetch_node`, `reconcile_node`, `approval_node`,
`escalate_node`) driven by a loop in `run_all`. No graph library: the control
flow (fetch → validate → classify → approve, with a bounded 3-attempt retry)
is linear, and a LangGraph `StateGraph` would only add dependency weight for
this shape of problem — the starter's docstring suggestion to use LangGraph
was deliberately not followed. `starter/ledger_api.py` is a stub of an
internal ledger service that deliberately misbehaves in specific, documented
ways — it is the grading fixture, not something to fix.

The task: for each of the payments in `data/processor_payments.csv`, fetch
the matching ledger entry via `ledger_api.fetch_ledger_entry(order_ref)`,
reconcile it against `data/reconciliation_policy.md` (and the extended
requirements in `docs/specifications/payment-reconciliation.md`), and produce
a reconciliation report where every payment ends up in exactly one bucket:
reconciled, exception, awaiting approval, or escalated.

A packaged Claude Code skill at
`.claude/skills/payment-reconciliation/SKILL.md` captures the implementation
rules (non-negotiables, per-stage design, response-validation approach,
testing requirements) in more detail than this file — read it before making
further changes to the agent.

## Environment

- Python 3.14, venv at `.venv` (`C:\Python314`).
- Dependencies (`pydantic`, `pytest`, `pytest-asyncio`, `pyright`, `ruff`) are
  declared in `pyproject.toml` — the one source of truth for the dependency
  list. Install with `pip install pydantic pytest pytest-asyncio pyright ruff`
  into `.venv` (the package itself isn't set up for `pip install -e .`; the
  repo mixes a `data/` and `starter/` top-level layout that setuptools can't
  auto-discover as a single package).
- Type-check with `python -m pyright` (strict mode, scoped to
  `starter/agent_skeleton.py` and `starter/test_agent_skeleton.py` via
  `pyrightconfig.json` — `starter/ledger_api.py` is excluded since it's the
  untyped, do-not-modify grading fixture).
- Lint with `python -m ruff check starter/agent_skeleton.py
  starter/test_agent_skeleton.py`.
- Run tests with `python -m pytest starter/test_agent_skeleton.py -v`
  (`asyncio_mode = "auto"` is set in `pyproject.toml`, so async tests need no
  per-test marker).
- Run the agent directly with `python starter/agent_skeleton.py` — prompts
  interactively for period/today only when stdin is a TTY; otherwise infers
  the period from the data and uses the real current date. `run_all(period=,
  today=, decide=)` accepts these as plain parameters for programmatic/test
  use.

## Architecture

The code structure and functionality should be guided by the following:

- Spec-Driven Development: Spec discipline governs agent behaviour and hand-offs, not just a single file.
- AI-Powered Development: Advanced Claude Code (agent modes, CLI, skills) delivering working software.
- LLM Evaluation & Interpretability: Objective evaluation (LLM-as-judge, scored comparisons); data-grounded decisions.
- Agent Design, Orchestration & Ops: Multi-agent / multi-step orchestration with failure handling, recovery, observability.
- AI Tool Integration & Extensibility: Extended agents via MCP servers, hooks, or custom skills/tools — secure and reusable.

**`starter/ledger_api.py` is read-only / do-not-modify.** It stands in for a
real service maintained by another team; the grader swaps in a version with
*additional* undisclosed misbehaviours and runs the agent unchanged against
it. Any fix that special-cases today's known quirks (rather than validating
the response shape generically) will fail against the grading variant.

## Deliverables and checklist

Required artifacts from an actual run (not hand-written): `ESCALATION.md` and
a `REFLECTION.md` (600–1000 words, six sections, weighted toward the failure
analysis). See `criteria_checklist.md` for the full deliverables and
self-review checklist before considering this done.
