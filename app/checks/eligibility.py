"""Reimbursement eligibility (docs/policy.md): policy status, service, patient, dates, currency, amount."""

from app.checks.base import PolicyStatus, collect, parse_date, same_name
from app.checks.money import parse_money
from app.workflow.states import Outcome

EUR = "EUR"


def check_eligibility(policy, *, patient_name, service_category, service_date, invoice_date, expense,
                      currency=EUR):
    """Every reason not to reimburse, in rule order. Facts only; nothing is inferred or defaulted."""
    period = policy["coverage_period"]
    start, end = parse_date(period["start"]), parse_date(period["end"])
    served, invoiced, amount = parse_date(service_date), parse_date(invoice_date), parse_money(expense)
    service = (service_category or "").strip()
    insured = any(same_name(person["name"], patient_name) for person in policy["insured_people"])
    excluded = service in policy["excluded_services"]
    return collect((
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
        ((currency or "").strip().upper() != EUR, Outcome.INVALID_INPUT, "Only EUR invoices can be reimbursed."),
        (amount is None, Outcome.INVALID_INPUT, "The invoice amount is missing or not a valid EUR amount."),
    ))
