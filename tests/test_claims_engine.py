"""Deterministic Claims engine: every policy calculation and rejection rule, no model, no MCP."""

import copy
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.claims_engine import (
    ClaimFacts, assess, calculate_reimbursement, facts_from_extraction, find_duplicate, missing_information,
    money, parse_amount, requires_authorization, round_half_up,
)
from app.contracts import ROOT

FIXTURES = json.loads((ROOT / "data" / "initial.json").read_text(encoding="utf-8"))


def policy(policy_id="POL-001", **terms):
    value = copy.deepcopy(FIXTURES["policies"][policy_id])
    value["financial_terms"].update(terms)
    return value


def history(policy_id="POL-001"):
    return [copy.deepcopy(c) for c in FIXTURES["claims"].values() if c["policy_id"] == policy_id]


SPECIALIST = ClaimFacts(
    service_category="specialist_consultation", has_invoice=True, provider_id="PROVIDER-002",
    invoice_number="INV-2026-008", invoice_date=date(2026, 10, 20), service_date=date(2026, 10, 20),
    patient_name="Taylor Example", service_description="specialist consultation",
    expense=Decimal("180.00"), currency="EUR", paid=True)

PHYSIO = replace(SPECIALIST, service_category="physiotherapy", service_description="physiotherapy",
                 has_prescription=True, prescription_patient_name="Taylor Example",
                 prescription_service_category="physiotherapy")


# --- Calculation ---------------------------------------------------------------------------------

@pytest.mark.parametrize("expense,deductible,remaining,eligible,theoretical,final,capped", [
    ("180.00", "50.00", "2896.00", "130.00", "104.00", "104.00", False),     # policy.md example 1
    ("1925.00", "50.00", "2896.00", "1875.00", "1500.00", "1500.00", False),  # example 2
    ("2000.00", "50.00", "2896.00", "1950.00", "1560.00", "1560.00", False),  # example 3
    ("2000.00", "50.00", "900.00", "1950.00", "1560.00", "900.00", True),     # remaining limit 900
    ("180.00", "100.00", "2896.00", "80.00", "64.00", "64.00", False),        # current deductible, not fixture
    ("180.00", "500.00", "2896.00", "0.00", "0.00", "0.00", False),           # expense below deductible
    ("50.00", "50.00", "2896.00", "0.00", "0.00", "0.00", False),             # expense equals deductible
    ("180.00", "50.00", "0.00", "130.00", "104.00", "0.00", True),            # limit exhausted
    ("100.63", "50.00", "2896.00", "50.63", "40.50", "40.50", False),         # 40.504 -> 40.50
    ("100.64", "50.00", "2896.00", "50.64", "40.51", "40.51", False),         # 40.512 -> 40.51
    ("100.62", "50.00", "2896.00", "50.62", "40.50", "40.50", False),         # 40.496 -> 40.50
])
def test_reimbursement_formula(expense, deductible, remaining, eligible, theoretical, final, capped):
    calc = calculate_reimbursement(expense, deductible, remaining)
    assert calc.as_dict() == {"expense": expense, "deductible": deductible, "eligible_base": eligible,
                              "theoretical": theoretical, "remaining_limit": f"{Decimal(remaining):.2f}",
                              "final": final, "capped_by_remaining_limit": capped}
    assert all(isinstance(v, Decimal) for v in (calc.eligible_base, calc.theoretical, calc.final))


@pytest.mark.parametrize("value,expected", [("0.125", "0.13"), ("2.675", "2.68"), ("1.005", "1.01"), ("0.124", "0.12")])
def test_rounding_is_decimal_half_up_not_float_or_bankers(value, expected):
    # Python's round() gives 0.12, 2.67 and 1.0 for the first three (banker's rounding / float error).
    assert round_half_up(Decimal(value)) == Decimal(expected)


def test_money_refuses_floats():
    with pytest.raises(TypeError):
        money(180.0)


@pytest.mark.parametrize("final,needed", [("1500.00", False), ("1500.01", True), ("1499.99", False), ("1560.00", True)])
def test_authorization_only_strictly_above_threshold(final, needed):
    assert requires_authorization(final, "1500.00") is needed


# --- Decisions: approvals and payments --------------------------------------------------------------

def test_specialist_180_is_paid_104_autonomously():
    decision = assess(SPECIALIST, policy(), history())
    assert (decision.action, decision.agent_state, decision.request_outcome) == ("pay", "completed", "paid")
    assert decision.calculation.final == Decimal("104.00")


def test_final_equal_to_threshold_is_autonomous():
    decision = assess(replace(PHYSIO, expense=Decimal("1925.00"), service_date=date(2026, 12, 31),
                              invoice_date=date(2026, 12, 31)), policy(), history())
    assert (decision.action, str(decision.calculation.final)) == ("pay", "1500.00")


def test_final_above_threshold_requires_authorization():
    decision = assess(replace(SPECIALIST, expense=Decimal("2000.00")), policy(), history())
    assert (decision.action, decision.agent_state, decision.request_outcome) == (
        "request_authorization", "awaiting_authorization", "authorization_requested")
    assert decision.calculation.final == Decimal("1560.00")


def test_remaining_limit_caps_and_avoids_authorization():
    facts = replace(SPECIALIST, service_category="diagnostic_test", patient_name="Morgan Sample",
                    expense=Decimal("2000.00"), has_prescription=True, prescription_patient_name="Morgan Sample",
                    prescription_service_category="diagnostic_test")
    decision = assess(facts, policy("POL-002"), history("POL-002"))
    assert (decision.action, str(decision.calculation.final)) == ("pay", "900.00")
    assert decision.calculation.capped_by_remaining_limit


def test_uses_current_policy_terms_not_fixture_defaults():
    decision = assess(SPECIALIST, policy(deductible="100.00", remaining_limit="50.00"), history())
    assert str(decision.calculation.final) == "50.00"


@pytest.mark.parametrize("terms", [{"deductible": "500.00"}, {"remaining_limit": "0.00"}])
def test_zero_reimbursement_is_registered_not_paid(terms):
    decision = assess(SPECIALIST, policy(**terms), history())
    assert (decision.action, decision.request_outcome) == ("register_zero", "zero_reimbursement")


# --- Decisions: rejections ----------------------------------------------------------------------------

@pytest.mark.parametrize("service", ["dental_care", "cosmetic_treatment", "medicines"])
def test_exclusions_close_without_asking_for_documents(service):
    decision = assess(ClaimFacts(service_category=service), policy(), history())
    assert (decision.action, decision.agent_state, decision.request_outcome) == (
        "close", "closed_without_execution", "not_covered")
    assert decision.missing == []


def test_exclusion_wins_over_inactive_policy_and_missing_documents():
    assert assess(ClaimFacts(service_category="dental_care"), policy("POL-003"), []).request_outcome == "not_covered"


def test_unlisted_service_is_not_covered():
    assert assess(replace(SPECIALIST, service_category="other"), policy(), history()).request_outcome == "not_covered"


@pytest.mark.parametrize("status", ["inactive", "terminated"])
def test_inactive_policy_is_rejected(status):
    value = policy("POL-003") if status == "inactive" else {**policy(), "status": "terminated"}
    decision = assess(replace(SPECIALIST, patient_name="Casey Demo"), value, [])
    assert (decision.action, decision.request_outcome) == ("close", "inactive_policy")


def test_missing_invoice_asks_once_for_everything():
    decision = assess(ClaimFacts(service_category="physiotherapy"), policy(), history())
    assert (decision.action, decision.agent_state, decision.request_outcome) == (
        "ask", "awaiting_clarification", "information_requested")
    assert len(decision.missing) == 2 and "invoice" in decision.missing[0] and "prescription" in decision.missing[1]


def test_all_missing_invoice_fields_are_listed_together():
    facts = replace(SPECIALIST, provider_id=None, invoice_number=None, expense=None, paid=None)
    missing = missing_information(facts)
    assert missing == ["the provider ID on the invoice", "the invoice number on the invoice",
                       "the invoice amount in EUR", "confirmation on the invoice that it has been paid in full"]


@pytest.mark.parametrize("change,expected", [
    ({"paid": False}, "paid in full"),
    ({"currency": "USD"}, "amount in EUR"),
    ({"expense": Decimal("0.00")}, "amount in EUR"),
    ({"service_date": None}, "service date"),
    ({"patient_name": None}, "patient's name"),
    ({"service_category": None}, "which medical service"),
])
def test_insufficient_invoice_contents_ask(change, expected):
    decision = assess(replace(SPECIALIST, **change), policy(), history())
    assert decision.action == "ask" and any(expected in item for item in decision.missing)


def test_specialist_needs_no_prescription():
    assert missing_information(SPECIALIST) == []


@pytest.mark.parametrize("change,expected", [
    ({"has_prescription": False, "prescription_patient_name": None, "prescription_service_category": None},
     "the prescription for this service"),
    ({"prescription_patient_name": "Jamie Example"}, "prescription issued for the invoiced patient"),
    ({"prescription_service_category": "diagnostic_test"}, "prescription for the invoiced service"),
    ({"prescription_service_category": None}, "prescribed service"),
])
def test_prescription_rules_for_diagnostics_and_physiotherapy(change, expected):
    decision = assess(replace(PHYSIO, **change), policy(), history())
    assert decision.action == "ask" and any(expected in item for item in decision.missing)


def test_insured_dependant_is_accepted_and_stranger_rejected():
    assert assess(replace(SPECIALIST, patient_name="jamie  example"), policy(), history()).action == "pay"
    decision = assess(replace(SPECIALIST, patient_name="Alex Stranger"), policy(), history())
    assert (decision.action, decision.request_outcome) == ("close", "not_insured")


@pytest.mark.parametrize("service_date,outcome", [
    (date(2026, 1, 1), "paid"), (date(2026, 12, 31), "paid"),
    (date(2025, 12, 31), "outside_coverage_period"), (date(2027, 1, 1), "outside_coverage_period"),
])
def test_coverage_period_includes_first_and_last_day(service_date, outcome):
    facts = replace(SPECIALIST, service_date=service_date, invoice_date=max(service_date, date(2026, 1, 1)))
    assert assess(facts, policy(), history()).request_outcome == outcome


def test_invoice_before_service_is_invalid():
    facts = replace(SPECIALIST, invoice_date=date(2026, 10, 19))
    assert assess(facts, policy(), history()).request_outcome == "invalid_input"


def test_same_day_invoice_is_valid():
    assert assess(SPECIALIST, policy(), history()).request_outcome == "paid"


def test_duplicate_of_paid_history_reports_existing_reference():
    facts = replace(SPECIALIST, provider_id="PROVIDER-001", invoice_number="INV-2026-001",
                    invoice_date=date(2026, 10, 2), service_date=date(2026, 10, 1))
    decision = assess(facts, policy(), history())
    assert (decision.action, decision.request_outcome, decision.duplicate_of) == ("close", "duplicate", "CL-H001")


@pytest.mark.parametrize("status,duplicate", [
    ("paid", True), ("registered", True), ("pending_authorization", True), ("payment_failed", True),
    ("rejected", False), ("not_covered", False), ("invalid", False), ("incomplete", False),
])
def test_duplicate_matches_paid_or_pending_records_only(status, duplicate):
    claims = history()
    claims[0]["status"] = status
    facts = replace(SPECIALIST, provider_id="provider-001", invoice_number="INV-2026-001", invoice_date=date(2026, 3, 9))
    assert (find_duplicate(facts, claims) is not None) is duplicate


@pytest.mark.parametrize("change", [
    {"provider_id": "PROVIDER-009"}, {"invoice_number": "INV-2026-999"}, {"invoice_date": date(2027, 10, 2)},
])
def test_duplicate_key_needs_provider_number_and_year(change):
    key = {"provider_id": "PROVIDER-001", "invoice_number": "INV-2026-001", "invoice_date": date(2026, 10, 2)}
    facts = replace(SPECIALIST, **{**key, **change})
    assert find_duplicate(facts, history()) is None


def test_multiple_services_or_patients_are_out_of_scope():
    decision = assess(replace(SPECIALIST, multiple_services_or_patients=True), policy(), history())
    assert (decision.request_category, decision.request_outcome) == ("out_of_scope_other", "out_of_scope")


# --- Untrusted extraction -> typed facts ----------------------------------------------------------------

T08_INVOICE = ("Provider ID: PROVIDER-002\nInvoice number: INV-2026-008\nInvoice date: 2026-10-20\n"
               "Service date: 2026-10-20\nPatient: Taylor Example\nService: specialist consultation\n"
               "Amount: EUR 180.00\nPayment status: PAID IN FULL")
EXTRACTED = {"service_category": "specialist_consultation", "multiple_services_or_patients": False,
             "provider_id": "PROVIDER-002", "invoice_number": "INV-2026-008", "invoice_date": "2026-10-20",
             "service_date": "2026-10-20", "patient_name": "Taylor Example",
             "service_description": "specialist consultation", "amount": "180.00", "currency": "EUR", "paid": True}


def test_extraction_becomes_typed_facts():
    facts = facts_from_extraction(EXTRACTED, {"invoice_text": T08_INVOICE})
    assert facts == SPECIALIST


def test_invoice_fields_are_discarded_when_no_invoice_text_was_supplied():
    facts = facts_from_extraction(EXTRACTED, {"invoice_text": None})
    assert not facts.has_invoice and facts.provider_id is None and facts.expense is None


def test_identifiers_not_in_the_document_are_treated_as_missing():
    facts = facts_from_extraction({**EXTRACTED, "invoice_number": "INV-HALLUCINATED"}, {"invoice_text": T08_INVOICE})
    assert facts.invoice_number is None
    assert "the invoice number on the invoice" in missing_information(facts)


@pytest.mark.parametrize("raw,expected", [("180", "180.00"), ("1,925.00", "1925.00"), ("EUR 180.5", "180.50"),
                                          ("abc", None), ("-5", None), ("", None)])
def test_amount_parsing(raw, expected):
    assert parse_amount(raw) == (Decimal(expected) if expected else None)


def test_invalid_dates_and_unknown_services_become_missing():
    facts = facts_from_extraction({**EXTRACTED, "invoice_date": "20/10/2026", "service_category": "massage"},
                                  {"invoice_text": T08_INVOICE})
    assert facts.invoice_date is None and facts.service_category is None
