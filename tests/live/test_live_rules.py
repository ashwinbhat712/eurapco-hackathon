"""Offline tests of the live rules against the real domain fixtures (no model)."""

import asyncio
from types import SimpleNamespace

import pytest

from app.domain import DomainStore
from app.live.rules import LiveRules
from app.workflow.ports import Action
from app.workflow.states import Agent, Outcome


class Reader:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    async def call(self, name, args):
        return self.store.call(name, {"run_id": self.run_id, **args})


class Agentish:
    async def compose_message(self, workflow, results):
        return "answer"


INVOICE = {"patient_name": "Jamie Example", "service_description": "Cardiology consultation",
           "provider_id": "PROVIDER-009", "invoice_number": "INV-9", "invoice_date": "2026-03-02",
           "service_date": "2026-03-01", "expense": "180.00", "currency": "EUR", "invoice_paid": True,
           "service_category": "specialist_consultation"}


def workflow(category, facts, policy_id="POL-001", agent=Agent.CLAIMS):
    return SimpleNamespace(request_category=category, facts=facts, policy_id=policy_id, ticket_id="T99",
                           handling_agent=agent, clarification=None,
                           original_request={"text": "x", "invoice_text": "invoice"})


@pytest.fixture
def decide(tmp_path):
    store = DomainStore(tmp_path)
    run_id = store.start_run()
    rules = LiveRules({agent: Agentish() for agent in Agent})
    return lambda wf: asyncio.run(rules.decide(wf, Reader(store, run_id)))


@pytest.fixture
def check():
    return LiveRules({}).check_information


class TestInformation:
    def test_address_without_postal_code_asks_for_it(self, check):
        result = check(workflow("contact_update", {"street": "8 Orchard Road", "city": "Turin", "country": "Italy"}))
        assert result.missing == ("postal_code",) and "postal code" in result.question

    def test_phone_alone_is_enough(self, check):
        assert check(workflow("contact_update", {"phone": "+393330000000"})).missing == ()

    def test_excluded_service_closes_without_asking(self, check):
        result = check(workflow("reimbursement_request", {"service_category": "dental_care"}))
        assert result.close == Outcome.NOT_COVERED and result.missing == ()

    def test_diagnostic_test_needs_prescription(self, check):
        facts = {**INVOICE, "service_category": "diagnostic_test"}
        assert check(workflow("reimbursement_request", facts)).missing == ("prescription_patient",
                                                                          "prescription_service")

    def test_prescription_for_another_service_is_asked_for_again(self, check):
        facts = {**INVOICE, "service_category": "diagnostic_test", "prescription_patient": "Jamie Example",
                 "prescription_service": "physiotherapy"}
        result = check(workflow("reimbursement_request", facts))
        assert result.missing == ("prescription_service",) and "same service" in result.question

    def test_matching_prescription_is_enough(self, check):
        facts = {**INVOICE, "service_category": "physiotherapy", "prescription_patient": "Jamie Example",
                 "prescription_service": "physiotherapy"}
        assert check(workflow("reimbursement_request", facts)).missing == ()

    def test_unpaid_invoice_is_missing_payment_proof(self, check):
        assert "invoice_paid" in check(workflow("reimbursement_request", {**INVOICE, "invoice_paid": False})).missing

    def test_termination_needs_explicit_instruction(self, check):
        assert check(workflow("policy_termination", {})).missing == ("explicit_instruction",)


class TestAdministration:
    def test_coverage_within_thresholds_executes(self, decide):
        decision = decide(workflow("coverage_change", {"annual_limit": "5000", "deductible": "200.00"}))
        assert decision.action == Action.EXECUTE
        assert decision.calls[0].args["changes"] == {"annual_limit": "5000.00", "deductible": "200.00"}

    def test_coverage_above_thresholds_asks_insurer(self, decide):
        decision = decide(workflow("coverage_change", {"annual_limit": "10000.00", "deductible": "500.00"}))
        assert decision.action == Action.ASK_AUTHORIZATION and decision.authorization.outcome == Outcome.UPDATED

    @pytest.mark.parametrize("facts", [{"annual_limit": "7000.00"}, {"deductible": "50.00", "annual_limit": "2000"}])
    def test_unsupported_coverage_closes(self, decide, facts):
        assert decide(workflow("coverage_change", facts)).outcome == Outcome.INVALID_INPUT

    def test_same_coverage_is_no_change(self, decide):
        assert decide(workflow("coverage_change", {"annual_limit": "3000.00"})).action == Action.NO_CHANGE

    def test_same_phone_is_no_change(self, decide):
        assert decide(workflow("contact_update", {"phone": "+393331234567"})).action == Action.NO_CHANGE

    def test_termination_asks_insurer(self, decide):
        decision = decide(workflow("policy_termination", {"explicit_instruction": True}))
        assert decision.authorization.on_approve[0].tool == "terminate_policy"

    def test_inactive_policy_closes(self, decide):
        decision = decide(workflow("contact_update", {"phone": "+393330000000"}, policy_id="POL-003"))
        assert decision.outcome == Outcome.INACTIVE_POLICY

    def test_enquiry_answers(self, decide):
        assert decide(workflow("policy_enquiry", {"question": "q"})).message == "answer"


class TestReimbursement:
    def test_small_claim_creates_and_pays(self, decide):
        decision = decide(workflow("reimbursement_request", INVOICE))
        assert [call.tool for call in decision.calls] == ["create_claim", "pay_claim"]
        assert decision.calls[1].args["amount"] == "104.00"
        assert decision.calls[0].args["evidence_refs"] == ["T99:invoice_text"]

    def test_large_claim_asks_insurer_after_pending_record(self, decide):
        decision = decide(workflow("reimbursement_request", {**INVOICE, "expense": "2000.00"}))
        assert [call.tool for call in decision.calls] == ["create_claim", "record_claim_outcome"]
        assert decision.authorization.on_approve[0].args["amount"] == "1560.00"

    def test_remaining_limit_caps_payment(self, decide):
        facts = {**INVOICE, "expense": "2000.00", "patient_name": "Jamie Sample"}
        decision = decide(workflow("reimbursement_request", facts, policy_id="POL-002"))
        assert decision.action == Action.EXECUTE and decision.calls[1].args["amount"] == "900.00"

    def test_duplicate_reports_existing_claim(self, decide):
        facts = {**INVOICE, "provider_id": "PROVIDER-001", "invoice_number": "INV-2026-001"}
        decision = decide(workflow("reimbursement_request", facts))
        assert decision.outcome == Outcome.DUPLICATE and "CL-H001" in decision.message

    @pytest.mark.parametrize(("facts", "outcome"), [
        ({"patient_name": "Someone Else"}, Outcome.NOT_INSURED),
        ({"service_date": "2025-12-31", "invoice_date": "2026-01-02"}, Outcome.OUTSIDE_COVERAGE_PERIOD),
        ({"invoice_date": "2026-02-01"}, Outcome.INVALID_INPUT),
        ({"expense": "40.00"}, Outcome.ZERO_REIMBURSEMENT)])
    def test_business_closures(self, decide, facts, outcome):
        assert decide(workflow("reimbursement_request", {**INVOICE, **facts})).outcome == outcome

    def test_inactive_policy(self, decide):
        facts = {**INVOICE, "patient_name": "Jamie Demo"}
        assert decide(workflow("reimbursement_request", facts, policy_id="POL-003")).outcome == Outcome.INACTIVE_POLICY
