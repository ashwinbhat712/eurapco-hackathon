"""Unit tests for the deterministic administration rules (challenge Part 4)."""

import copy
import json

import pytest

from agents.administration.extraction import AdminIntent, normalize_money
from agents.administration.ports import WorkflowState
from agents.administration.rules import decide, decide_insurer_decision
from app.contracts import ROOT

FIXTURES = json.loads((ROOT / "data" / "initial.json").read_text(encoding="utf-8"))["policies"]


def policy(policy_id="POL-001", **overrides):
    record = copy.deepcopy(FIXTURES[policy_id])
    terms = overrides.pop("financial_terms", None)
    if terms:
        record["financial_terms"].update(terms)
    record.update(overrides)
    return record


def workflow(clarification_asked=False, phase="initial"):
    return WorkflowState(ticket_id="T-TEST", run_id="RUN-TEST", policy_id="POL-001",
                         phase=phase, clarification_asked=clarification_asked)


def intent(**payload):
    payload.setdefault("summary", "test intent")
    return AdminIntent.from_payload(payload)


def triple(decision):
    return decision.request_category, decision.agent_state, decision.request_outcome


# --- contact update -------------------------------------------------------

def test_phone_change_writes_only_the_changed_field():
    decision = decide(intent(request_category="contact_update", contact_fields=["phone"],
                             phone="+393339998888"), policy(), workflow())
    assert triple(decision) == ("contact_update", "completed", "updated")
    assert decision.effect == {"tool": "update_contact_details",
                               "args": {"policy_id": "POL-001", "changes": {"phone": "+393339998888"}}}


def test_identical_values_are_no_change_without_a_tool_call():
    decision = decide(intent(request_category="contact_update", contact_fields=["email", "phone"],
                             email="taylor@example.com", phone="+393331234567"), policy(), workflow())
    assert triple(decision) == ("contact_update", "completed", "no_change")
    assert decision.effect is None


def test_mixed_request_writes_only_the_changed_value():
    decision = decide(intent(request_category="contact_update", contact_fields=["email", "phone"],
                             email="new@example.com", phone="+393331234567"), policy(), workflow())
    assert decision.effect["args"]["changes"] == {"email": "new@example.com"}


def test_address_missing_postal_code_asks_once_for_everything_missing():
    decision = decide(intent(request_category="contact_update", contact_fields=["address", "email"],
                             address={"street": "8 Orchard Road", "city": "Turin", "country": "Italy"}),
                      policy(), workflow())
    assert triple(decision) == ("contact_update", "awaiting_clarification", "information_requested")
    assert "postal code" in decision.question
    assert "email" in decision.question


def test_address_completed_by_the_reply_is_written_in_full():
    decision = decide(intent(request_category="contact_update", contact_fields=["address"],
                             address={"street": "8 Orchard Road", "city": "Turin",
                                      "postal_code": "10100", "country": "Italy"}),
                      policy(), workflow(clarification_asked=True))
    assert triple(decision) == ("contact_update", "completed", "updated")
    assert decision.effect["args"]["changes"]["address"]["postal_code"] == "10100"


def test_reply_still_incomplete_closes_without_a_second_question():
    decision = decide(intent(request_category="contact_update", contact_fields=["address"],
                             address={"street": "8 Orchard Road", "city": "Turin", "country": "Italy"}),
                      policy(), workflow(clarification_asked=True))
    assert triple(decision) == ("contact_update", "closed_without_execution", "insufficient_information")
    assert decision.question is None


def test_phone_not_in_international_format_is_invalid_input():
    decision = decide(intent(request_category="contact_update", contact_fields=["phone"],
                             phone="3339998888"), policy(), workflow())
    assert triple(decision) == ("contact_update", "closed_without_execution", "invalid_input")
    assert decision.effect is None


def test_invalid_email_is_invalid_input():
    decision = decide(intent(request_category="contact_update", contact_fields=["email"],
                             email="taylor(at)example.com"), policy(), workflow())
    assert triple(decision) == ("contact_update", "closed_without_execution", "invalid_input")


def test_iban_only_request_is_out_of_scope_iban():
    decision = decide(intent(request_category="contact_update", contact_fields=["iban"]), policy(), workflow())
    assert triple(decision) == ("out_of_scope_iban", "closed_without_execution", "out_of_scope")


def test_iban_plus_supported_change_applies_only_the_supported_part():
    decision = decide(intent(request_category="contact_update", contact_fields=["iban", "email"],
                             email="new@example.com"), policy(), workflow())
    assert triple(decision) == ("contact_update", "completed", "updated")
    assert decision.effect["args"]["changes"] == {"email": "new@example.com"}
    assert any("IBAN" in note for note in decision.notes)


def test_contact_update_on_an_inactive_policy_is_closed():
    decision = decide(intent(request_category="contact_update", contact_fields=["phone"], phone="+393339998888"),
                      policy("POL-003"), workflow())
    assert triple(decision) == ("contact_update", "closed_without_execution", "inactive_policy")
    assert decision.effect is None


def test_contact_update_without_any_value_asks_once():
    decision = decide(intent(request_category="contact_update", contact_fields=[]), policy(), workflow())
    assert triple(decision) == ("contact_update", "awaiting_clarification", "information_requested")


# --- coverage change ------------------------------------------------------

def test_limit_5000_and_deductible_200_are_autonomous():
    decision = decide(intent(request_category="coverage_change", annual_limit="5000", deductible="200"),
                      policy(), workflow())
    assert triple(decision) == ("coverage_change", "completed", "updated")
    assert decision.effect["args"]["changes"] == {"annual_limit": "5000.00", "deductible": "200.00"}


def test_limit_10000_and_deductible_500_need_one_combined_proposal():
    decision = decide(intent(request_category="coverage_change", annual_limit="10000", deductible="500"),
                      policy(), workflow())
    assert triple(decision) == ("coverage_change", "awaiting_authorization", "authorization_requested")
    assert decision.proposal["tool"] == "update_coverage"
    assert decision.proposal["args"]["changes"] == {"annual_limit": "10000.00", "deductible": "500.00"}
    assert decision.effect is None


def test_single_field_above_the_level_still_needs_authorization():
    decision = decide(intent(request_category="coverage_change", annual_limit="10000"), policy(), workflow())
    assert triple(decision) == ("coverage_change", "awaiting_authorization", "authorization_requested")
    assert decision.proposal["details"]["final_values"] == {"annual_limit": "10000.00", "deductible": "50.00"}


def test_final_value_comparison_uses_the_current_policy_not_the_increment():
    current = policy(financial_terms={"annual_limit": "5000.00", "deductible": "200.00"})
    decision = decide(intent(request_category="coverage_change", deductible="500"), current, workflow())
    assert triple(decision) == ("coverage_change", "awaiting_authorization", "authorization_requested")
    assert decision.proposal["args"]["changes"] == {"deductible": "500.00"}


def test_value_outside_the_catalog_is_invalid_input():
    decision = decide(intent(request_category="coverage_change", annual_limit="4000"), policy(), workflow())
    assert triple(decision) == ("coverage_change", "closed_without_execution", "invalid_input")
    assert decision.effect is None and decision.proposal is None


def test_decrease_is_invalid_input_and_keeps_the_category():
    current = policy(financial_terms={"annual_limit": "5000.00"})
    decision = decide(intent(request_category="coverage_change", annual_limit="3000"), current, workflow())
    assert triple(decision) == ("coverage_change", "closed_without_execution", "invalid_input")


def test_combined_change_with_one_bad_component_writes_nothing():
    decision = decide(intent(request_category="coverage_change", annual_limit="10000", deductible="20"),
                      policy(), workflow())
    assert triple(decision) == ("coverage_change", "closed_without_execution", "invalid_input")
    assert decision.effect is None and decision.proposal is None


def test_requested_values_already_in_force_are_no_change():
    decision = decide(intent(request_category="coverage_change", annual_limit="3000", deductible="50"),
                      policy(), workflow())
    assert triple(decision) == ("coverage_change", "completed", "no_change")


def test_coverage_change_without_values_asks_once_then_closes():
    asked = decide(intent(request_category="coverage_change", coverage_fields=["annual_limit"]),
                   policy(), workflow())
    assert triple(asked) == ("coverage_change", "awaiting_clarification", "information_requested")
    closed = decide(intent(request_category="coverage_change", coverage_fields=["annual_limit"]),
                    policy(), workflow(clarification_asked=True))
    assert triple(closed) == ("coverage_change", "closed_without_execution", "insufficient_information")


def test_coverage_change_on_an_inactive_policy_is_closed():
    decision = decide(intent(request_category="coverage_change", annual_limit="10000"),
                      policy("POL-003"), workflow())
    assert triple(decision) == ("coverage_change", "closed_without_execution", "inactive_policy")


# --- termination ----------------------------------------------------------

def test_explicit_termination_always_requires_authorization():
    decision = decide(intent(request_category="policy_termination", explicit_termination=True),
                      policy(), workflow())
    assert triple(decision) == ("policy_termination", "awaiting_authorization", "authorization_requested")
    assert decision.proposal["tool"] == "terminate_policy"
    assert decision.effect is None


def test_question_about_termination_is_an_enquiry():
    decision = decide(intent(request_category="policy_termination", termination_question=True),
                      policy(), workflow())
    assert triple(decision) == ("policy_enquiry", "completed", "answered")
    assert decision.proposal is None and decision.effect is None


def test_termination_without_explicit_instruction_is_an_enquiry():
    decision = decide(intent(request_category="policy_termination"), policy(), workflow())
    assert triple(decision) == ("policy_enquiry", "completed", "answered")


def test_already_terminated_policy_is_no_change():
    decision = decide(intent(request_category="policy_termination", explicit_termination=True),
                      policy(status="terminated"), workflow())
    assert triple(decision) == ("policy_termination", "completed", "no_change")


def test_termination_of_an_inactive_policy_is_closed():
    decision = decide(intent(request_category="policy_termination", explicit_termination=True),
                      policy("POL-003"), workflow())
    assert triple(decision) == ("policy_termination", "closed_without_execution", "inactive_policy")


# --- insurer decisions ----------------------------------------------------

def test_approval_executes_exactly_the_frozen_call():
    proposal = {"tool": "update_coverage",
                "args": {"policy_id": "POL-001", "changes": {"annual_limit": "10000.00"}}}
    decision = decide_insurer_decision(proposal, {"decision": "approve"}, policy(), "coverage_change")
    assert triple(decision) == ("coverage_change", "completed", "updated")
    assert decision.effect["args"] == proposal["args"]


def test_rejection_executes_nothing_and_reports_the_reason():
    proposal = {"tool": "terminate_policy", "args": {"policy_id": "POL-001"}}
    decision = decide_insurer_decision(proposal, {"decision": "reject", "reason": "Declined for training."},
                                       policy(), "policy_termination")
    assert triple(decision) == ("policy_termination", "closed_without_execution", "insurer_rejected")
    assert decision.effect is None
    assert "Declined for training." in decision.message


def test_approved_termination_executes_terminate_policy():
    proposal = {"tool": "terminate_policy", "args": {"policy_id": "POL-001"}}
    decision = decide_insurer_decision(proposal, {"decision": "approve"}, policy(), "policy_termination")
    assert triple(decision) == ("policy_termination", "completed", "terminated")
    assert decision.effect["tool"] == "terminate_policy"


# --- enquiry, out of scope and normalization ------------------------------

def test_policy_enquiry_answers_from_the_retrieved_record_only():
    decision = decide(intent(request_category="policy_enquiry"), policy(), workflow())
    assert triple(decision) == ("policy_enquiry", "completed", "answered")
    assert "2896.00" in decision.message and decision.effect is None


def test_claims_request_is_out_of_scope_and_flagged_for_rerouting():
    decision = decide(intent(request_category="out_of_scope_claims"), policy(), workflow())
    assert triple(decision) == ("out_of_scope_claims", "closed_without_execution", "out_of_scope")
    assert decision.reroute == "claims"


def test_unclear_request_closes_without_a_question():
    decision = decide(intent(request_category="unclear"), policy(), workflow())
    assert triple(decision) == ("unclear", "closed_without_execution", "unclear")
    assert decision.question is None


@pytest.mark.parametrize("raw,expected", [("5000", "5000.00"), ("EUR 10,000", "10000.00"),
                                          ("200.00", "200.00"), ("as high as possible", None), (None, None)])
def test_money_normalization(raw, expected):
    assert normalize_money(raw) == expected


def test_unknown_category_falls_back_to_unclear():
    assert AdminIntent.from_payload({"request_category": "nonsense"}).request_category == "unclear"
