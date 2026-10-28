"""Reimbursement eligibility (docs/policy.md): policy status, service, patient, dates, currency, amount."""

from app.checks.base import CheckResult, IsoDate, PolicyStatus, Same
from app.checks.money import Money
from app.workflow.states import Outcome


class EligibilityCheck:
    """Every reason not to reimburse an invoice on one policy, in rule order. Facts only; nothing is inferred."""

    def __init__(self, policy):
        self.policy = policy

    def check(self, *, patient_name, service_category, service_date, invoice_date, expense,
              currency=Money.CURRENCY):
        policy, period = self.policy, self.policy["coverage_period"]
        start, end = IsoDate.parse(period["start"]), IsoDate.parse(period["end"])
        served, invoiced = IsoDate.parse(service_date), IsoDate.parse(invoice_date)
        service = str(service_category or "").strip()
        insured = any(Same.name(person["name"], patient_name) for person in policy["insured_people"])
        excluded = service in policy["excluded_services"]
        return CheckResult.collect((
            (policy["status"] != PolicyStatus.ACTIVE, Outcome.INACTIVE_POLICY, f"The policy is {policy['status']}."),
            (excluded, Outcome.NOT_COVERED, f"{service} is excluded by the policy."),
            (not excluded and service not in policy["covered_services"], Outcome.NOT_COVERED,
             f"{service or 'The service'} is not a covered service."),
            (not insured, Outcome.NOT_INSURED, f"{patient_name or 'The patient'} is not insured on this policy."),
            (served is None, Outcome.INVALID_INPUT, "The service date is missing or not a YYYY-MM-DD date."),
            (invoiced is None, Outcome.INVALID_INPUT, "The invoice date is missing or not a YYYY-MM-DD date."),
            (served is not None and not start <= served <= end, Outcome.OUTSIDE_COVERAGE_PERIOD,
             f"The service date {service_date} is outside the coverage period {period['start']} to {period['end']}."),
            (served is not None and invoiced is not None and invoiced < served, Outcome.INVALID_INPUT,
             "The invoice date is before the service date."),
            (str(currency or "").strip().upper() != Money.CURRENCY, Outcome.INVALID_INPUT,
             "Only EUR invoices can be reimbursed."),
            (Money.parse(expense) is None, Outcome.INVALID_INPUT,
             "The invoice amount is missing or not a valid EUR amount."),
        ))
