# ABOUTME: Async payment reconciliation agent — fetches ledger entries, validates responses
# ABOUTME: against the documented contract, classifies exceptions, and gates them on human approval.
"""Payment reconciliation agent.

Plain async functions, one per stage (fetch -> reconcile -> approve, with
escalation on exhausted retries) driven by a loop in run_all. No graph
library: the control flow is linear and a StateGraph would only add
dependency weight for this shape of problem. pydantic validates ledger
responses against the documented contract; pytest-asyncio drives the tests.
"""
import asyncio
import csv
import logging
import sys
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import TypedDict, cast

from pydantic import BaseModel, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_api

_untyped_fetch = cast(
    Callable[[str], Awaitable[object]],
    ledger_api.fetch_ledger_entry,  # pyright: ignore[reportUnknownMemberType] - untyped, do-not-modify stub
)


async def fetch_ledger_entry(order_ref: str) -> object:
    """Typed wrapper around the untyped ledger_api stub (do not modify that file)."""
    return await _untyped_fetch(order_ref)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PAYMENTS = DATA_DIR / "processor_payments.csv"
POLICY = DATA_DIR / "reconciliation_policy.md"
ESCALATION_PATH = Path(__file__).resolve().parent.parent / "ESCALATION.md"
LOG_PATH = Path(__file__).resolve().parent.parent / "reconciliation.log"

AMOUNT_TOLERANCE = 0.02
MAX_ATTEMPTS = 3

logger = logging.getLogger("reconciliation")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False


class PaymentRow(TypedDict):
    """A row of data/processor_payments.csv, as returned by csv.DictReader."""

    payment_id: str
    order_ref: str
    amount: str
    currency: str
    settled_date: str
    method: str


class LedgerEntryDict(TypedDict):
    """LedgerEntry, dumped to JSON-safe primitives via model_dump(mode='json')."""

    entry_id: str
    order_ref: str
    amount: float
    currency: str
    posted_date: str
    entry_type: str


class ReconState(TypedDict, total=True):
    payment_id: str
    order_ref: str
    amount: float
    currency: str
    settled_date: str
    ledger_entry: LedgerEntryDict | None
    attempts: int
    attempt_log: list[str]
    last_error: str | None
    not_found: bool
    duplicate_payment_ids: list[str]
    status: str              # reconciled | exception | awaiting_approval | escalated
    exception_class: str | None
    evidence: str


class ReconciliationReport(TypedDict):
    period: str
    today: str
    total: int
    reconciled: list[ReconState]
    exceptions: list[ReconState]
    exceptions_by_class: dict[str, list[ReconState]]
    awaiting_approval: list[ReconState]
    escalated: list[ReconState]


class LedgerEntry(BaseModel):
    """The documented ledger contract, exactly as specified in ledger_api.fetch_ledger_entry."""

    entry_id: str
    order_ref: str
    amount: float
    currency: str
    posted_date: date
    entry_type: str


class LedgerNotFound(Exception):
    """The ledger has no entry for this order_ref — a documented, well-formed answer, not a failure."""


class LedgerResponseInvalid(Exception):
    """The response does not match the documented contract shape."""


def _looks_like_entry(candidate: object) -> bool:
    return isinstance(candidate, dict) and "entry_id" in candidate and "order_ref" in candidate


def validate_response(raw: object) -> LedgerEntryDict:
    """Validate a ledger response against the documented contract.

    Returns a normalised entry (dates and numbers as JSON-safe values), or
    raises LedgerNotFound / LedgerResponseInvalid so the caller can retry
    with the reason attached. A response that parses as JSON is not
    automatically trusted: an envelope other than the flat contract shape,
    or a field of the wrong type, is treated as a fact about the response,
    never coerced into something that merely looks usable.
    """
    if not isinstance(raw, dict):
        raise LedgerResponseInvalid(f"expected a JSON object, got {type(raw).__name__}")
    raw = cast(dict[str, object], raw)

    if raw.get("error") == "not_found":
        raise LedgerNotFound(str(raw.get("order_ref", "<unknown>")))

    candidate: object = raw
    if not _looks_like_entry(candidate):
        nested = raw.get("data")
        candidate = cast(dict[str, object], nested).get("entry") if isinstance(nested, dict) else None
        if not _looks_like_entry(candidate):
            raise LedgerResponseInvalid(f"unrecognised response shape: keys={sorted(raw.keys())}")

    try:
        entry = LedgerEntry.model_validate(candidate)
    except ValidationError as exc:
        raise LedgerResponseInvalid(str(exc)) from exc

    return LedgerEntryDict(**entry.model_dump(mode="json"))


async def fetch_node(state: ReconState) -> ReconState:
    """Call the ledger service once and validate the response.

    On failure, records the reason and increments attempts; the retry/
    escalation policy lives in run_all's loop, not here, so this stays a
    single, independently testable attempt.
    """
    order_ref = state["order_ref"]
    attempts = state.get("attempts", 0) + 1

    try:
        raw = await fetch_ledger_entry(order_ref)
        entry = validate_response(raw)
    except LedgerNotFound:
        return {**state, "ledger_entry": None, "attempts": attempts, "last_error": None, "not_found": True}
    except Exception as exc:  # noqa: BLE001 - any failure here is a fact to record, not to swallow
        reason = f"{type(exc).__name__}: {exc}"
        return {**state, "ledger_entry": None, "attempts": attempts, "last_error": reason, "not_found": False}

    return {**state, "ledger_entry": entry, "attempts": attempts, "last_error": None, "not_found": False}


def reconcile_node(state: ReconState) -> ReconState:
    """Apply the matching rules and set status plus exception_class.

    Duplicate settlement takes priority over amount/currency checks, since
    it is an anomaly regardless of whether the amounts happen to agree.
    Posting-vs-settlement timing is recorded as evidence but never gates a
    decision here: policy #3 treats a post within 3 days (including across
    a month boundary) as normal, so nothing in this function is allowed to
    turn that gap into an exception.
    """
    evidence: list[str] = []
    duplicates = state.get("duplicate_payment_ids") or []
    entry = state.get("ledger_entry")

    if duplicates:
        exception_class = "duplicate_settlement"
        evidence.append(
            f"order_ref {state['order_ref']} also claimed by payment(s) {', '.join(duplicates)}"
        )
    elif state.get("not_found"):
        exception_class = "unmatched_payment"
        evidence.append(f"no ledger entry found for order_ref {state['order_ref']}")
    else:
        assert entry is not None, "reconcile_node reached the matched branch without a ledger entry"
        amount_diff = abs(state["amount"] - entry["amount"])
        evidence.append(
            f"payment {state['amount']} {state['currency']} vs ledger {entry['amount']} "
            f"{entry['currency']} (diff {amount_diff:.2f})"
        )
        posted = date.fromisoformat(entry["posted_date"])
        settled = date.fromisoformat(state["settled_date"])
        evidence.append(
            f"settled {settled.isoformat()}, posted {posted.isoformat()} "
            f"({abs((posted - settled).days)}d — informational, not a matching criterion)"
        )

        if state["currency"] != entry["currency"]:
            exception_class = "currency_mismatch"
        elif amount_diff > AMOUNT_TOLERANCE:
            exception_class = "amount_mismatch"
        else:
            exception_class = None

    status = "reconciled" if exception_class is None else "exception"
    return {**state, "status": status, "exception_class": exception_class, "evidence": "; ".join(evidence)}


async def _prompt_for_decision(state: ReconState) -> str | None:
    """Default approval decision: a real, blocking prompt when a human is present.

    Returns None (no decision made) when stdin is not a TTY, so an
    unattended run halts the record at "awaiting_approval" instead of
    guessing an answer nobody gave.
    """
    if not sys.stdin.isatty():
        return None

    print(f"\nException: {state['payment_id']} / {state['order_ref']} — {state['exception_class']}")
    print(f"Evidence: {state['evidence']}")
    while True:
        raw = input("Accept as exception, or reject (a/r)? ").strip().lower()
        if raw in ("a", "accept"):
            return "accept"
        if raw in ("r", "reject"):
            return "reject"
        print("Please answer 'a' (accept) or 'r' (reject).")


async def approval_node(
    state: ReconState,
    decide: Callable[[ReconState], Awaitable[str | None]] | None = None,
) -> ReconState:
    """The human validation gate. Halts before any exception reaches the close file.

    - accept: the exception is confirmed; status stays "exception".
    - reject: Rina judges the record fine after all; reclassified as reconciled.
    - no decision (no human present): halts at "awaiting_approval" rather than
      resolving on the agent's own authority.
    """
    if state["status"] != "exception":
        return state

    verdict = await (decide or _prompt_for_decision)(state)

    if verdict is None:
        return {**state, "status": "awaiting_approval"}
    if verdict == "accept":
        return state
    if verdict == "reject":
        return {
            **state,
            "status": "reconciled",
            "exception_class": None,
            "evidence": state["evidence"] + "; Rina rejected the exception — reclassified as reconciled",
        }
    raise ValueError(f"unexpected approval verdict: {verdict!r}")


async def escalate_node(state: ReconState) -> ReconState:
    """Write the record, every attempt and its outcome, to ESCALATION.md."""
    attempt_log = state.get("attempt_log", [])
    lines = [
        f"## {state['payment_id']} / {state['order_ref']}",
        "",
        f"- Payment: {state['amount']} {state['currency']}, settled {state['settled_date']}",
        f"- Attempts: {len(attempt_log)}",
    ]
    lines.extend(f"  {i}. {err}" for i, err in enumerate(attempt_log, start=1))
    lines.append(
        "- To proceed: a ledger response for this order_ref matching the documented "
        "contract (see the last attempt above for what was wrong with the latest one)."
    )
    lines.append("")

    with open(ESCALATION_PATH, "a", encoding="utf-8") as fh:  # noqa: ASYNC230 - sequential pipeline, no concurrent I/O to block
        fh.write("\n".join(lines) + "\n")

    return {**state, "status": "escalated"}


async def _process_payment(
    payment: PaymentRow,
    duplicate_payment_ids: list[str],
    decide: Callable[[ReconState], Awaitable[str | None]] | None = None,
) -> ReconState:
    state: ReconState = {
        "payment_id": payment["payment_id"],
        "order_ref": payment["order_ref"],
        "amount": float(payment["amount"]),
        "currency": payment["currency"],
        "settled_date": payment["settled_date"],
        "ledger_entry": None,
        "attempts": 0,
        "attempt_log": [],
        "last_error": None,
        "not_found": False,
        "duplicate_payment_ids": duplicate_payment_ids,
        "status": "",
        "exception_class": None,
        "evidence": "",
    }

    while True:
        state = await fetch_node(state)
        logger.info(
            "fetch payment_id=%s order_ref=%s attempt=%d ok=%s",
            state["payment_id"], state["order_ref"], state["attempts"], state["last_error"] is None,
        )
        if state["last_error"] is None:
            break

        state["attempt_log"].append(state["last_error"])
        if state["attempts"] >= MAX_ATTEMPTS:
            state = await escalate_node(state)
            logger.info(
                "escalate payment_id=%s order_ref=%s attempts=%d",
                state["payment_id"], state["order_ref"], len(state["attempt_log"]),
            )
            return state

        logger.info(
            "retry payment_id=%s order_ref=%s previous_error=%r",
            state["payment_id"], state["order_ref"], state["last_error"],
        )

    state = reconcile_node(state)
    logger.info(
        "reconcile payment_id=%s status=%s exception_class=%s",
        state["payment_id"], state["status"], state["exception_class"],
    )
    state = await approval_node(state, decide=decide)
    logger.info("approve payment_id=%s status=%s", state["payment_id"], state["status"])
    return state


def _load_payments() -> list[PaymentRow]:
    with open(PAYMENTS, encoding="utf-8", newline="") as fh:
        return [PaymentRow(**row) for row in csv.DictReader(fh)]


def _resolve_period(period: str | None, payments: list[PaymentRow]) -> str:
    inferred = payments[0]["settled_date"][:7] if payments else ""
    if period:
        return period
    if sys.stdin.isatty():
        raw = input(f"Reconciliation period (YYYY-MM) [{inferred}]: ").strip()
        return raw or inferred
    return inferred


def _resolve_today(today: str | None) -> date:
    if today:
        return date.fromisoformat(today)
    real_today = date.today()  # noqa: DTZ011 - naive date, consistent with settled_date/posted_date elsewhere
    if sys.stdin.isatty():
        raw = input(f"Reference (today's) date [{real_today.isoformat()}]: ").strip()
        return date.fromisoformat(raw) if raw else real_today
    return real_today


async def run_all(
    period: str | None = None,
    today: str | None = None,
    decide: Callable[[ReconState], Awaitable[str | None]] | None = None,
) -> ReconciliationReport:
    """Run every payment in the resolved period through the pipeline.

    period/today are plain optional parameters so tests and callers never
    touch stdin; the interactive prompts only fire when nothing was passed
    in and a human is actually present (sys.stdin.isatty()).
    """
    all_payments = _load_payments()
    period = _resolve_period(period, all_payments)
    today_date = _resolve_today(today)
    logger.info("run_started period=%s today=%s", period, today_date.isoformat())

    payments = [p for p in all_payments if p["settled_date"].startswith(period)]

    order_ref_owners: dict[str, list[str]] = defaultdict(list)
    for p in payments:
        order_ref_owners[p["order_ref"]].append(p["payment_id"])

    records: list[ReconState] = []
    for p in payments:
        siblings = [pid for pid in order_ref_owners[p["order_ref"]] if pid != p["payment_id"]]
        records.append(await _process_payment(p, siblings, decide=decide))

    reconciled = [r for r in records if r["status"] == "reconciled"]
    exceptions = [r for r in records if r["status"] == "exception"]
    awaiting_approval = [r for r in records if r["status"] == "awaiting_approval"]
    escalated = [r for r in records if r["status"] == "escalated"]

    total_accounted = len(reconciled) + len(exceptions) + len(awaiting_approval) + len(escalated)
    assert total_accounted == len(records), (
        f"record count mismatch: {total_accounted} accounted for vs {len(records)} input payments"
    )

    exceptions_by_class: dict[str, list[ReconState]] = defaultdict(list)
    for r in exceptions:
        exception_class = r["exception_class"]
        assert exception_class is not None, "an 'exception' record must carry an exception_class"
        exceptions_by_class[exception_class].append(r)

    logger.info(
        "run_complete total=%d reconciled=%d exceptions=%d awaiting_approval=%d escalated=%d",
        len(records), len(reconciled), len(exceptions), len(awaiting_approval), len(escalated),
    )

    return {
        "period": period,
        "today": today_date.isoformat(),
        "total": len(records),
        "reconciled": reconciled,
        "exceptions": exceptions,
        "exceptions_by_class": dict(exceptions_by_class),
        "awaiting_approval": awaiting_approval,
        "escalated": escalated,
    }


def _print_report(report: ReconciliationReport) -> None:
    print(f"\nReconciliation report — period {report['period']}, reference date {report['today']}")
    print(f"Total payments: {report['total']}")
    print(f"  Reconciled: {len(report['reconciled'])}")
    print(f"  Exceptions: {len(report['exceptions'])}")
    for exception_class, group in report["exceptions_by_class"].items():
        print(f"    {exception_class}: {len(group)}")
    print(f"  Awaiting approval: {len(report['awaiting_approval'])}")
    print(f"  Escalated: {len(report['escalated'])}")


if __name__ == "__main__":
    report = asyncio.run(run_all())
    _print_report(report)
