# ABOUTME: pytest-asyncio suite for the reconciliation agent — covers validation,
# ABOUTME: fetch/retry, reconciliation rules, the approval gate, escalation, and the full loop.
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger_api
from agent_skeleton import (
    ESCALATION_PATH,
    LedgerEntryDict,
    LedgerNotFound,
    LedgerResponseInvalid,
    ReconState,
    approval_node,
    escalate_node,
    fetch_node,
    reconcile_node,
    run_all,
    validate_response,
)


@pytest.fixture(autouse=True)
def _reset_ledger_and_escalation() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction] - autouse fixture
    ledger_api.reset()
    if ESCALATION_PATH.exists():
        ESCALATION_PATH.unlink()
    yield
    ledger_api.reset()
    if ESCALATION_PATH.exists():
        ESCALATION_PATH.unlink()


def make_ledger_entry(**overrides: object) -> LedgerEntryDict:
    entry: LedgerEntryDict = {
        "entry_id": "LED-1",
        "order_ref": "ORD-TEST",
        "amount": 100.0,
        "currency": "GBP",
        "posted_date": "2026-03-10",
        "entry_type": "sale",
    }
    entry.update(overrides)  # type: ignore[typeddict-item]
    return entry


def make_state(**overrides: object) -> ReconState:
    state: ReconState = {
        "payment_id": "PAY-TEST",
        "order_ref": "ORD-TEST",
        "amount": 100.0,
        "currency": "GBP",
        "settled_date": "2026-03-10",
        "ledger_entry": None,
        "attempts": 0,
        "attempt_log": [],
        "last_error": None,
        "not_found": False,
        "duplicate_payment_ids": [],
        "status": "",
        "exception_class": None,
        "evidence": "",
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


async def _accept(_state: ReconState) -> str:
    return "accept"


async def _reject(_state: ReconState) -> str:
    return "reject"


async def _no_one_present(_state: ReconState) -> None:
    return None


# --- validate_response: response-validation approach ---------------------

class TestValidateResponse:
    def test_flat_contract_shape_accepted(self) -> None:
        entry = validate_response({
            "entry_id": "LED-1", "order_ref": "ORD-1", "amount": 100.0,
            "currency": "GBP", "posted_date": "2026-03-10", "entry_type": "sale",
        })
        assert entry["amount"] == 100.0
        assert entry["currency"] == "GBP"

    def test_not_found_raises_ledger_not_found(self) -> None:
        with pytest.raises(LedgerNotFound):
            validate_response({"error": "not_found", "order_ref": "ORD-999"})

    def test_nested_envelope_is_unwrapped_generically(self) -> None:
        """ORD-70009/ORD-70022 shape: {"data": {"entry": {...}}, "meta": {...}}."""
        entry = validate_response({
            "data": {"entry": {
                "entry_id": "LED-9", "order_ref": "ORD-9", "amount": 28.2,
                "currency": "GBP", "posted_date": "2026-03-06", "entry_type": "sale",
            }},
            "meta": {"schema": "v2"},
        })
        assert entry["amount"] == 28.2
        assert entry["entry_id"] == "LED-9"

    def test_null_required_field_rejected(self) -> None:
        """ORD-70005 shape: currency arrives as null."""
        with pytest.raises(LedgerResponseInvalid):
            validate_response({
                "entry_id": "LED-5", "order_ref": "ORD-5", "amount": 312.75,
                "currency": None, "posted_date": "2026-03-04", "entry_type": "sale",
            })

    def test_non_iso_date_rejected(self) -> None:
        """ORD-70016 shape: posted_date reformatted to DD/MM/YYYY."""
        with pytest.raises(LedgerResponseInvalid):
            validate_response({
                "entry_id": "LED-16", "order_ref": "ORD-16", "amount": 300.0,
                "currency": "GBP", "posted_date": "09/03/2026", "entry_type": "sale",
            })

    def test_formatted_numeric_string_amount_accepted(self) -> None:
        """ORD-70003/ORD-70019 shape: amount arrives as a formatted string."""
        entry = validate_response({
            "entry_id": "LED-3", "order_ref": "ORD-3", "amount": "1240.00",
            "currency": "GBP", "posted_date": "2026-03-03", "entry_type": "sale",
        })
        assert entry["amount"] == 1240.0

    def test_non_dict_response_rejected(self) -> None:
        with pytest.raises(LedgerResponseInvalid):
            validate_response("not a dict")

    def test_unrecognised_shape_rejected(self) -> None:
        with pytest.raises(LedgerResponseInvalid):
            validate_response({"unexpected": "shape"})


# --- fetch_node: the ledger call, with responses mocked -------------------

class TestFetchNode:
    async def test_successful_fetch_populates_ledger_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_fetch(order_ref: str) -> object:
            return {
                "entry_id": "LED-1", "order_ref": order_ref, "amount": 100.0,
                "currency": "GBP", "posted_date": "2026-03-10", "entry_type": "sale",
            }

        monkeypatch.setattr("agent_skeleton._untyped_fetch", fake_fetch)
        state = await fetch_node(make_state())
        entry = state["ledger_entry"]
        assert entry is not None
        assert entry["amount"] == 100.0
        assert state["last_error"] is None
        assert state["attempts"] == 1

    async def test_not_found_sets_not_found_flag_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_fetch(order_ref: str) -> object:
            return {"error": "not_found", "order_ref": order_ref}

        monkeypatch.setattr("agent_skeleton._untyped_fetch", fake_fetch)
        state = await fetch_node(make_state())
        assert state["not_found"] is True
        assert state["last_error"] is None
        assert state["ledger_entry"] is None

    async def test_invalid_shape_records_reason_and_increments_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_fetch(order_ref: str) -> object:
            return {"unexpected": "shape"}

        monkeypatch.setattr("agent_skeleton._untyped_fetch", fake_fetch)
        state = await fetch_node(make_state(attempts=1))
        assert state["last_error"] is not None
        assert "unrecognised response shape" in state["last_error"]
        assert state["attempts"] == 2

    async def test_transport_exception_records_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_fetch(order_ref: str) -> object:
            raise TimeoutError(f"ledger service timeout for {order_ref}")

        monkeypatch.setattr("agent_skeleton._untyped_fetch", fake_fetch)
        state = await fetch_node(make_state())
        assert state["last_error"] == "TimeoutError: ledger service timeout for ORD-TEST"
        assert state["ledger_entry"] is None

    async def test_real_stub_transient_timeout_then_success(self) -> None:
        """ORD-70020: fails twice, succeeds on the 3rd attempt against the real stub."""
        state = make_state(order_ref="ORD-70020")
        for expected_ok in (False, False, True):
            state = await fetch_node(state)
            assert (state["last_error"] is None) == expected_ok


# --- reconcile_node: matching rules ----------------------------------------

class TestReconcileNode:
    def test_within_tolerance_reconciles(self) -> None:
        state = make_state(amount=100.0, ledger_entry=make_ledger_entry(amount=100.01))
        result = reconcile_node(state)
        assert result["status"] == "reconciled"
        assert result["exception_class"] is None

    def test_beyond_tolerance_is_amount_mismatch(self) -> None:
        state = make_state(amount=100.0, ledger_entry=make_ledger_entry(amount=100.05))
        result = reconcile_node(state)
        assert result["status"] == "exception"
        assert result["exception_class"] == "amount_mismatch"

    def test_currency_mismatch_takes_priority_over_amount(self) -> None:
        state = make_state(amount=100.0, currency="EUR", ledger_entry=make_ledger_entry(amount=100.0))
        result = reconcile_node(state)
        assert result["exception_class"] == "currency_mismatch"

    def test_posting_within_three_days_is_not_an_exception(self) -> None:
        """Policy #3: a post within 3 days of settlement, including across a month
        boundary, is timing, not a discrepancy."""
        state = make_state(
            settled_date="2026-03-31",
            amount=275.0,
            ledger_entry=make_ledger_entry(amount=275.0, posted_date="2026-04-01"),
        )
        result = reconcile_node(state)
        assert result["status"] == "reconciled"

    def test_unmatched_payment_when_not_found(self) -> None:
        state = make_state(not_found=True)
        result = reconcile_node(state)
        assert result["exception_class"] == "unmatched_payment"

    def test_duplicate_settlement_takes_priority(self) -> None:
        state = make_state(
            duplicate_payment_ids=["PAY-OTHER"],
            ledger_entry=make_ledger_entry(amount=100.0),
        )
        result = reconcile_node(state)
        assert result["exception_class"] == "duplicate_settlement"

    def test_refund_reconciles_normally(self) -> None:
        state = make_state(
            amount=-45.99,
            ledger_entry=make_ledger_entry(amount=-45.99, entry_type="refund"),
        )
        result = reconcile_node(state)
        assert result["status"] == "reconciled"


# --- approval_node: the human validation gate ------------------------------

class TestApprovalNode:
    async def test_non_exception_status_passes_through_untouched(self) -> None:
        state = make_state(status="reconciled")
        result = await approval_node(state)
        assert result["status"] == "reconciled"

    async def test_accept_keeps_exception_status(self) -> None:
        state = make_state(status="exception", exception_class="amount_mismatch")
        result = await approval_node(state, decide=_accept)
        assert result["status"] == "exception"

    async def test_reject_reclassifies_as_reconciled(self) -> None:
        state = make_state(status="exception", exception_class="amount_mismatch", evidence="diff 0.03")
        result = await approval_node(state, decide=_reject)
        assert result["status"] == "reconciled"
        assert result["exception_class"] is None
        assert "rejected" in result["evidence"]

    async def test_no_decision_halts_at_awaiting_approval(self) -> None:
        """No human present: the gate must halt, not resolve on its own authority."""
        state = make_state(status="exception", exception_class="amount_mismatch")
        result = await approval_node(state, decide=_no_one_present)
        assert result["status"] == "awaiting_approval"

    async def test_default_gate_genuinely_blocks_on_input_when_interactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proves the default path is a real blocking prompt, not a rubber stamp."""
        state = make_state(status="exception", exception_class="amount_mismatch", evidence="diff 0.03")
        calls: list[str] = []

        def fake_input(prompt: str) -> str:
            calls.append(prompt)
            return "a"

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", fake_input)
        result = await approval_node(state)
        assert calls, "approval_node must call input() when a human is present"
        assert result["status"] == "exception"


# --- escalate_node: ESCALATION.md -------------------------------------------

class TestEscalateNode:
    async def test_writes_record_and_every_attempt_to_escalation_file(self) -> None:
        state = make_state(
            order_ref="ORD-70023",
            attempt_log=[
                "TimeoutError: ledger service timeout for ORD-70023",
                "TimeoutError: ledger service timeout for ORD-70023",
                "TimeoutError: ledger service timeout for ORD-70023",
            ],
        )
        result = await escalate_node(state)
        assert result["status"] == "escalated"

        content = ESCALATION_PATH.read_text(encoding="utf-8")
        assert "PAY-TEST" in content
        assert "ORD-70023" in content
        assert content.count("TimeoutError") == 3


# --- run_all: the full loop, and the two graded invariants -----------------

class TestRunAll:
    async def test_totals_add_up_to_input_count(self) -> None:
        report = await run_all(period="2026-03", today="2026-04-05", decide=_accept)
        accounted = (
            len(report["reconciled"]) + len(report["exceptions"])
            + len(report["awaiting_approval"]) + len(report["escalated"])
        )
        assert accounted == report["total"]
        assert report["total"] == 24

    async def test_no_unverified_record_is_marked_reconciled(self) -> None:
        """A record whose fetch never succeeded (escalated) must never appear
        in the reconciled bucket."""
        report = await run_all(period="2026-03", today="2026-04-05", decide=_accept)
        escalated_order_refs = {r["order_ref"] for r in report["escalated"]}
        reconciled_order_refs = {r["order_ref"] for r in report["reconciled"]}
        assert escalated_order_refs.isdisjoint(reconciled_order_refs)
        assert "ORD-70023" in escalated_order_refs  # persistent timeout, never verified

    async def test_persistent_failure_escalates_after_max_attempts(self) -> None:
        report = await run_all(period="2026-03", today="2026-04-05", decide=_accept)
        escalated = {r["order_ref"]: r for r in report["escalated"]}
        assert "ORD-70023" in escalated
        assert len(escalated["ORD-70023"]["attempt_log"]) == 3

    async def test_transient_failure_recovers_and_reconciles(self) -> None:
        report = await run_all(period="2026-03", today="2026-04-05", decide=_accept)
        all_records = (
            report["reconciled"] + report["exceptions"]
            + report["awaiting_approval"] + report["escalated"]
        )
        ord_70020 = next(r for r in all_records if r["order_ref"] == "ORD-70020")
        assert ord_70020["status"] != "escalated"

    async def test_duplicate_settlement_detected(self) -> None:
        """PAY-4010/PAY-4011 both claim ORD-70010."""
        report = await run_all(period="2026-03", today="2026-04-05", decide=_accept)
        duplicate_class = report["exceptions_by_class"].get("duplicate_settlement", [])
        order_refs = {r["order_ref"] for r in duplicate_class}
        assert "ORD-70010" in order_refs

    async def test_unmatched_payments_reported_not_dropped(self) -> None:
        """ORD-70012/ORD-70013 have no ledger entry at all."""
        report = await run_all(period="2026-03", today="2026-04-05", decide=_accept)
        unmatched_class = report["exceptions_by_class"].get("unmatched_payment", [])
        order_refs = {r["order_ref"] for r in unmatched_class}
        assert "ORD-70012" in order_refs
        assert "ORD-70013" in order_refs

    async def test_no_decision_leaves_records_awaiting_approval(self) -> None:
        report = await run_all(period="2026-03", today="2026-04-05", decide=_no_one_present)
        assert len(report["exceptions"]) == 0
        assert len(report["awaiting_approval"]) > 0
