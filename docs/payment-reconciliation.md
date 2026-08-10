# payment-reconciliation.md

This file provides the requirements, acceptance criteria and tasks needed for reconciling the payment settlements against an internal ledger.

## Objective

Build an agent that reconciles monthly settlements against the ledger, classifies the exceptions, and routes them to the reconciliation lead for approval. The reconciliation logic is not the only thing here. Very important to consider what the agent does when the ledger service does not behave: whether it notices, whether it recovers, whether it knows when to stop, and whether anything can silently disappear from the report.

## Requirements

- Reconcile every payment and produce a report whose totals add up to the number of input payments. A record that quietly vanishes from the report is the worst outcome in this case - worse than a wrong classification, because nobody knows to look for it.
- Validate every ledger response against the shape you expect before using it. A response that parses as JSON is not a response you can trust, and the contract in the stub is what the other team documented rather than a guarantee.
- Never invent a value to keep going. If a field is missing or wrong, that is a fact about the response, not a gap to fill in.
- Retry with the specific failure reason included, so the next attempt has some chance of differing from the last. After 3 failed attempts on a record, stop and escalate - write the record, every attempt and its outcome, and what you would need in order to proceed, to ESCALATION.md. Do not loop.
- Implement the human approval gate so it genuinely halts before an exception is written to the close file. Attach the needed evidence for the reconciliation lead to accept or reject without re-doing your work.
- Apply the tolerance and timing rules exactly. A difference inside tolerance is not an exception, and reporting it is a false positive that costs the reconciliation lead the same time as a miss.
- Write a pytest-asyncio suite covering the agent loop, the ledger call with the response mocked, each recovery path, and the escalation path. Include the passing output.
- Be observable. The reconciliation lead must be able to reconstruct, for any record, what was attempted and why it ended where it did.

## Tasks to be executed and their rules

For these use [reconciliation policy](../data/reconciliation_policy.md) numerals one to seven. This document references as Reconciliation period: March 2026; however, this is sandbox data and the agent should be flexible enough to process any period as long as there is available data for it. Therefore, as input data, the period (month and year) and today's reference should be asked for to the user. For the latter, if a date is not entered (YYYY-MM-DD) the current date should be used; shown to the user in the prompt (Reference (today's) date) at the end of it between parenthesis.

## Acceptance criteria

- Working agent, with the reconciliation report and its totals reconciling to the input count.
- ESCALATION.md as produced by an actual run.
- Response-validation approach, and the list of misbehaviours found in the ledger service.
- Evidence of the human approval gate halting, and what reconciliation lead sees when it does.
- Passing pytest-asyncio output covering the loop, mocked tool calls, recovery, and escalation.
- Observability evidence: traces or structured logs sufficient to reconstruct any record's path.
- REFLECTION.md, 600-1000 words, six sections. The failure sections carry the most weight.

