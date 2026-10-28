"""Edge cases of the pure checks in app/checks (no I/O, no model)."""

import copy
import json
from decimal import Decimal

import pytest

from app.checks.coverage import CoverageChangeCheck
from app.checks.duplicates import DuplicateFinder
from app.checks.eligibility import EligibilityCheck
from app.checks.money import Money
from app.checks.reimbursement import Reimbursement
from app.contracts import ROOT
from app.workflow.states import Outcome

INITIAL = json.loads((ROOT / "data" / "initial.json").read_text(encoding="utf-8"))


@pytest.fixture
def policy():
    return copy.deepcopy(INITIAL["policies"]["POL-001"])


class TestMoney:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("180", "180.00"), ("180.5", "180.50"), ("EUR 1,925.00", "1925.00"), ("1.925,00 €", "1925.00"),
        ("1925,5", "1925.50"), ("1 925,00", "1925.00"), ("1,925,000", "1925000.00"), ("0", "0.00"),
        (1925, "1925.00"), (Decimal("12.30"), "12.30"), ("12.30 euro", "12.30")])
    def test_reads(self, raw, expected):
        assert Money.parse(raw) == Decimal(expected)

    @pytest.mark.parametrize("raw", ["", None, True, "-5.00", "1.925", "1,925", "12.345", "1.2.3", "NaN",
                                     "Infinity", "abc", "1e3", 0.1 + 0.2, "5 000 000 , 00 ,"])
    def test_refuses_ambiguous_or_malformed(self, raw):
        assert Money.parse(raw) is None


class TestReimbursement:
    @pytest.mark.parametrize(("expense", "remaining", "final", "needs"), [
        ("180.00", "2896.00", "104.00", False), ("1925.00", "2896.00", "1500.00", False),  # equality: autonomous
        ("2000.00", "2896.00", "1560.00", True), ("2000.00", "900.00", "900.00", False),
        ("1925.01", "2896.00", "1500.01", True), ("50.00", "2896.00", "0.00", False),
        ("10.00", "2896.00", "0.00", False), ("180.00", "0.00", "0.00", False)])
    def test_formula(self, expense, remaining, final, needs):
        result = Reimbursement.calculate(Decimal(expense), Decimal("50.00"), Decimal(remaining), Decimal("1500.00"))
        assert (result.final, result.needs_authorization) == (Decimal(final), needs)
        assert result.zero == (result.final == 0)

    def test_half_up_rounding(self):
        # 50.01 - 50 = 0.01 -> 0.008 -> 0.01 (half-up), never banker's rounding.
        calculate = Reimbursement.calculate
        assert calculate(Decimal("50.01"), Decimal("50"), Decimal("100"), Decimal("1500")).final == Decimal("0.01")
        assert calculate(Decimal("50.07"), Decimal("50"), Decimal("100"), Decimal("1500")).theoretical == Decimal("0.06")

    def test_cap_is_flagged(self):
        result = Reimbursement.calculate(Decimal("2000"), Decimal("50"), Decimal("900"), Decimal("1500"))
        assert result.capped_by_remaining_limit and result.theoretical == Decimal("1560.00")

    @pytest.mark.parametrize("expense", [Decimal("-1"), None])
    def test_negative_or_missing_refused(self, expense):
        with pytest.raises(ValueError):
            Reimbursement.calculate(expense, Decimal("50"), Decimal("900"), Decimal("1500"))

    def test_for_policy_uses_current_terms(self, policy):
        policy["financial_terms"]["remaining_limit"] = "900.00"
        assert Reimbursement.for_policy(policy, "EUR 2,000.00").final == Decimal("900.00")

    def test_for_policy_refuses_unreadable_terms(self, policy):
        policy["financial_terms"]["deductible"] = "fifty"
        with pytest.raises(ValueError):
            Reimbursement.for_policy(policy, "180.00")

    def test_explain_shows_every_step(self):
        text = Reimbursement.calculate(Decimal("180"), Decimal("50"), Decimal("2896"), Decimal("1500")).explain()
        assert text == ("invoice EUR 180.00 minus deductible EUR 50.00 = EUR 130.00; "
                        "80% of that = EUR 104.00; reimbursement EUR 104.00.")

    def test_explain_names_the_cap(self):
        text = Reimbursement.calculate(Decimal("2000"), Decimal("50"), Decimal("900"), Decimal("1500")).explain()
        assert "capped at the remaining annual limit EUR 900.00; reimbursement EUR 900.00." in text


def eligibility(policy, **overrides):
    facts = {"patient_name": "Jamie Example", "service_category": "physiotherapy", "service_date": "2026-12-31",
             "invoice_date": "2027-01-02", "expense": "1925.00", "currency": "EUR", **overrides}
    return EligibilityCheck(policy).check(**facts)


class TestEligibility:
    def test_last_coverage_day_with_later_invoice_is_eligible(self, policy):
        assert eligibility(policy).ok

    def test_first_coverage_day_is_eligible(self, policy):
        assert eligibility(policy, service_date="2026-01-01", invoice_date="2026-01-01").ok

    @pytest.mark.parametrize(("overrides", "outcome"), [
        ({"service_date": "2025-12-31"}, Outcome.OUTSIDE_COVERAGE_PERIOD),
        ({"service_date": "2027-01-01", "invoice_date": "2027-01-01"}, Outcome.OUTSIDE_COVERAGE_PERIOD),
        ({"invoice_date": "2026-12-30"}, Outcome.INVALID_INPUT),
        ({"service_date": "31/12/2026"}, Outcome.INVALID_INPUT),
        ({"service_date": "2026-02-30"}, Outcome.INVALID_INPUT),
        ({"currency": "USD"}, Outcome.INVALID_INPUT),
        ({"expense": "1.925"}, Outcome.INVALID_INPUT),
        ({"patient_name": "Jamie Exampel"}, Outcome.NOT_INSURED),  # no fuzzy matching
        ({"patient_name": None}, Outcome.NOT_INSURED),
        ({"service_category": "dental_care"}, Outcome.NOT_COVERED),
        ({"service_category": "acupuncture"}, Outcome.NOT_COVERED),
        ({"service_category": None}, Outcome.NOT_COVERED)])
    def test_rejections(self, policy, overrides, outcome):
        assert eligibility(policy, **overrides).outcome == outcome

    def test_name_normalisation(self, policy):
        assert eligibility(policy, patient_name="  jamie   EXAMPLE ").ok

    @pytest.mark.parametrize("status", ["inactive", "terminated"])
    def test_inactive_policy_comes_first(self, policy, status):
        policy["status"] = status
        result = eligibility(policy, service_category="dental_care")
        assert result.outcome == Outcome.INACTIVE_POLICY and len(result.issues) == 2

    def test_lowercase_currency_accepted(self, policy):
        assert eligibility(policy, currency=" eur ").ok


HISTORY = INITIAL["claims"]


def claim(status, number="INV-1", date="2026-03-01", claim_id="CL-1"):
    return {"claim_id": claim_id, "status": status,
            "invoice": {"provider_id": "PROVIDER-001", "invoice_number": number, "invoice_date": date}}


class TestDuplicates:
    def test_history_claim_is_found(self):
        claims = list(HISTORY.values()) if isinstance(HISTORY, dict) else HISTORY
        result = DuplicateFinder(claims).find(provider_id="PROVIDER-001", invoice_number="INV-2026-001",
                                              invoice_date="2026-05-20")
        assert result.found and result.duplicate["claim_id"] == "CL-H001"

    def test_other_year_is_not_a_duplicate(self):
        assert not DuplicateFinder([claim("paid")]).find(provider_id="PROVIDER-001", invoice_number="INV-1",
                                                          invoice_date="2027-03-01").found

    @pytest.mark.parametrize("status", ["paid", "pending_authorization", "registered", "payment_failed"])
    def test_open_or_paid_records_block(self, status):
        assert DuplicateFinder([claim(status)]).find(provider_id=" PROVIDER-001 ", invoice_number="INV-1",
                                                     invoice_date="2026-12-31").found

    def test_closed_unpaid_record_is_reported_not_blocking(self):
        result = DuplicateFinder([claim("rejected")]).find(provider_id="PROVIDER-001", invoice_number="INV-1",
                                                            invoice_date="2026-01-01")
        assert not result.found and result.closed_matches == ("CL-1",)

    def test_paid_record_preferred(self):
        claims = [claim("pending_authorization", claim_id="CL-A"), claim("paid", claim_id="CL-B")]
        result = DuplicateFinder(claims).find(provider_id="PROVIDER-001", invoice_number="INV-1",
                                              invoice_date="2026-01-01")
        assert result.duplicate["claim_id"] == "CL-B"

    def test_case_matters_no_fuzzy_matching(self):
        assert not DuplicateFinder([claim("paid")]).find(provider_id="provider-001", invoice_number="INV-1",
                                                          invoice_date="2026-01-01").found

    @pytest.mark.parametrize("overrides", [{"invoice_date": "2026"}, {"provider_id": ""}, {"invoice_number": None}])
    def test_unreadable_key(self, overrides):
        key = {"provider_id": "PROVIDER-001", "invoice_number": "INV-1", "invoice_date": "2026-01-01", **overrides}
        assert DuplicateFinder([claim("paid")]).find(**key).invalid_key

    def test_malformed_history_is_skipped(self):
        broken = {"claim_id": "CL-X", "status": "paid", "invoice": None}
        finder = DuplicateFinder([broken, "not a claim", {}])
        assert not finder.find(provider_id="P", invoice_number="I", invoice_date="2026-01-01").found


class TestCoverage:
    def test_increase_within_thresholds(self, policy):
        check = CoverageChangeCheck(policy).check(annual_limit="5000", deductible="EUR 200")
        assert check.changes == {"annual_limit": "5000.00", "deductible": "200.00"} and not check.needs_authorization

    def test_above_threshold_needs_authorization(self, policy):
        assert CoverageChangeCheck(policy).check(annual_limit="10000.00").needs_authorization
        assert CoverageChangeCheck(policy).check(deductible="500.00").needs_authorization

    def test_equal_values_are_no_change(self, policy):
        assert CoverageChangeCheck(policy).check(annual_limit="3000.00", deductible="50").no_change

    def test_equal_field_is_dropped_and_not_judged(self, policy):
        policy["financial_terms"]["deductible"] = "500.00"
        check = CoverageChangeCheck(policy).check(annual_limit="5000.00", deductible="500.00")
        assert check.changes == {"annual_limit": "5000.00"} and not check.needs_authorization

    @pytest.mark.parametrize("values", [{"annual_limit": "7000"}, {"annual_limit": "abc"}, {},
                                        {"annual_limit": "1.000"}])
    def test_invalid(self, policy, values):
        assert CoverageChangeCheck(policy).check(**values).result.outcome == Outcome.INVALID_INPUT

    def test_decrease_rejects_the_whole_change(self, policy):
        policy["financial_terms"]["deductible"] = "200.00"
        check = CoverageChangeCheck(policy).check(annual_limit="5000.00", deductible="100.00")
        assert check.result.outcome == Outcome.INVALID_INPUT and check.changes == {}

    def test_inactive_policy(self, policy):
        policy["status"] = "inactive"
        assert CoverageChangeCheck(policy).check(annual_limit="5000").result.outcome == Outcome.INACTIVE_POLICY
