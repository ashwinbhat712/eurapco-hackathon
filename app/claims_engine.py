"""Deterministic Claims decision engine (docs/policy.md, docs/minimum-information.md).

No model is involved here: the LLM only extracts facts from text (app/claims_extraction.py).
Every eligibility, duplicate, money and authorization decision is plain Python over
facts, the current policy (get_policy) and the claim history (list_claims).
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
REIMBURSEMENT_RATE = Decimal("0.80")
PRESCRIPTION_REQUIRED = frozenset({"diagnostic_test", "physiotherapy"})
# A record in one of these states is "paid or pending": a new identical invoice is a duplicate.
DUPLICATE_BLOCKING_STATUSES = frozenset({"registered", "pending_authorization", "paid", "payment_failed"})


def money(value) -> Decimal:
    """Parse an exact EUR amount; floats are refused so no binary rounding error can enter."""
    if isinstance(value, float):
        raise TypeError("Use str or Decimal for money, never float.")
    return Decimal(str(value))


def round_half_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    return f"{round_half_up(value):.2f}"


def normalize_name(name) -> str:
    return " ".join(str(name or "").split()).casefold()


@dataclass(frozen=True)
class Calculation:
    expense: Decimal
    deductible: Decimal
    eligible_base: Decimal
    theoretical: Decimal
    remaining_limit: Decimal
    final: Decimal
    capped_by_remaining_limit: bool

    def as_dict(self):
        return {k: (money_str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


def calculate_reimbursement(expense, deductible, remaining_limit) -> Calculation:
    """eligible=max(0,expense-deductible); theoretical=80% half-up to cents; final=min(theoretical,remaining)."""
    expense, deductible, remaining = money(expense), money(deductible), money(remaining_limit)
    eligible = max(Decimal("0"), expense - deductible)
    theoretical = round_half_up(eligible * REIMBURSEMENT_RATE)
    remaining = max(Decimal("0"), remaining)
    final = round_half_up(min(theoretical, remaining))
    return Calculation(expense=round_half_up(expense), deductible=round_half_up(deductible),
                       eligible_base=round_half_up(eligible), theoretical=theoretical,
                       remaining_limit=round_half_up(remaining), final=final,
                       capped_by_remaining_limit=remaining < theoretical)


def requires_authorization(final_reimbursement, threshold) -> bool:
    """Only the FINAL reimbursement counts, and only when strictly greater than the threshold."""
    return money(final_reimbursement) > money(threshold)


@dataclass(frozen=True)
class ClaimFacts:
    """Facts for one reimbursement request, already validated and typed (see facts_from_extraction)."""

    service_category: str | None = None  # one of the six services, "other", or None when unknown
    has_invoice: bool = False
    provider_id: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    service_date: date | None = None
    patient_name: str | None = None
    service_description: str | None = None
    expense: Decimal | None = None
    currency: str | None = None
    paid: bool | None = None
    has_prescription: bool = False
    prescription_patient_name: str | None = None
    prescription_service_category: str | None = None
    multiple_services_or_patients: bool = False


@dataclass
class Decision:
    """The engine's verdict. `action` tells the workflow which (if any) effects are allowed."""

    action: str  # close | ask | register_zero | request_authorization | pay
    request_category: str
    agent_state: str
    request_outcome: str
    reason: str
    missing: list = field(default_factory=list)
    duplicate_of: str | None = None
    calculation: Calculation | None = None
    authorization_threshold: str | None = None

    def as_dict(self):
        data = asdict(self)
        data["calculation"] = self.calculation.as_dict() if self.calculation else None
        return data


def _close(outcome, reason, category="reimbursement_request", **extra):
    return Decision(action="close", request_category=category, agent_state="closed_without_execution",
                    request_outcome=outcome, reason=reason, **extra)


def missing_information(facts: ClaimFacts) -> list[str]:
    """Every missing or insufficient required content, so the one clarification asks for all of it."""
    if not facts.has_invoice:
        missing = ["the paid invoice (provider ID, invoice number, invoice date, service date, patient, "
                   "service description, amount in EUR and paid status)"]
        if facts.service_category in PRESCRIPTION_REQUIRED or facts.service_category is None:
            missing.append("the prescription naming the patient and the prescribed service"
                           + ("" if facts.service_category else " (required for diagnostic tests and physiotherapy)"))
        return missing

    missing = []
    for value, label in ((facts.provider_id, "the provider ID"), (facts.invoice_number, "the invoice number"),
                         (facts.invoice_date, "the invoice date"), (facts.service_date, "the service date"),
                         (facts.patient_name, "the patient's name"),
                         (facts.service_description, "the service description")):
        if not value:
            missing.append(label + " on the invoice")
    if facts.expense is None or facts.expense <= 0 or facts.currency != "EUR":
        missing.append("the invoice amount in EUR")
    if facts.paid is not True:
        missing.append("confirmation on the invoice that it has been paid in full")
    if facts.service_category is None:
        missing.append("which medical service was provided")

    if facts.service_category in PRESCRIPTION_REQUIRED:
        if not facts.has_prescription:
            missing.append("the prescription for this service")
        else:
            if not facts.prescription_patient_name:
                missing.append("the patient's name on the prescription")
            elif facts.patient_name and normalize_name(facts.prescription_patient_name) != normalize_name(facts.patient_name):
                missing.append(f"a prescription issued for the invoiced patient ({facts.patient_name})")
            if not facts.prescription_service_category:
                missing.append("the prescribed service on the prescription")
            elif facts.prescription_service_category != facts.service_category:
                missing.append("a prescription for the invoiced service")
    return missing


def duplicate_key(provider_id, invoice_number, invoice_date):
    year = invoice_date.year if isinstance(invoice_date, date) else str(invoice_date)[:4]
    return (str(provider_id).strip().casefold(), str(invoice_number).strip().casefold(), str(year))


def find_duplicate(facts: ClaimFacts, claims) -> dict | None:
    """Paid or pending record with the same provider ID + invoice number + invoice-date year."""
    key = duplicate_key(facts.provider_id, facts.invoice_number, facts.invoice_date)
    for claim in claims:
        invoice = claim.get("invoice") or {}
        if claim.get("status") not in DUPLICATE_BLOCKING_STATUSES:
            continue
        if duplicate_key(invoice.get("provider_id"), invoice.get("invoice_number"), invoice.get("invoice_date")) == key:
            return claim
    return None


def assess(facts: ClaimFacts, policy: dict, claims: list) -> Decision:
    """Apply the reimbursement rules in a fixed order; the first failing rule decides."""
    if facts.multiple_services_or_patients:
        return _close("out_of_scope", "Several services or patients in one request are outside the supported "
                      "reimbursement workflow; submit one service for one patient per request.",
                      category="out_of_scope_other")

    # 1. Covered? Exclusions need no evidence, no approval and no further questions.
    service = facts.service_category
    if service in policy["excluded_services"]:
        return _close("not_covered", f"{service.replace('_', ' ')} is excluded by the policy.")
    if service is not None and service not in policy["covered_services"]:
        return _close("not_covered", "The service is not among the covered services "
                      "(specialist consultation, diagnostic tests, physiotherapy).")

    # 2. Active policy?
    if policy["status"] != "active":
        return _close("inactive_policy", f"Policy {policy['policy_id']} is {policy['status']}, not active.")

    # 3. Complete documents? One clarification asking for everything at once.
    missing = missing_information(facts)
    if missing:
        return Decision(action="ask", request_category="reimbursement_request", agent_state="awaiting_clarification",
                        request_outcome="information_requested", reason="Required reimbursement information is missing.",
                        missing=missing)

    # 4. Insured patient?
    insured = {normalize_name(person["name"]) for person in policy["insured_people"]}
    if normalize_name(facts.patient_name) not in insured:
        return _close("not_insured", f"{facts.patient_name} is not an insured person on policy {policy['policy_id']}.")

    # 5. Service within coverage (both ends inclusive); invoice not before service.
    start = date.fromisoformat(policy["coverage_period"]["start"])
    end = date.fromisoformat(policy["coverage_period"]["end"])
    if not start <= facts.service_date <= end:
        return _close("outside_coverage_period", f"Service date {facts.service_date.isoformat()} is outside the "
                      f"coverage period {start.isoformat()} to {end.isoformat()}.")
    if facts.invoice_date < facts.service_date:
        return _close("invalid_input", f"Invoice date {facts.invoice_date.isoformat()} precedes service date "
                      f"{facts.service_date.isoformat()}.")

    # 6. Already claimed?
    duplicate = find_duplicate(facts, claims)
    if duplicate:
        return _close("duplicate", f"Invoice {facts.invoice_number} from {facts.provider_id} "
                      f"({facts.invoice_date.year}) is already recorded as {duplicate['claim_id']} "
                      f"with status {duplicate['status']}.", duplicate_of=duplicate["claim_id"])

    # 7. How much? Always from the CURRENT policy terms.
    terms = policy["financial_terms"]
    calculation = calculate_reimbursement(facts.expense, terms["deductible"], terms["remaining_limit"])
    threshold = policy["authorization_thresholds"]["reimbursement"]
    if calculation.final == 0:
        cause = ("the expense does not exceed the deductible" if calculation.eligible_base == 0
                 else "the remaining annual limit is exhausted")
        return Decision(action="register_zero", request_category="reimbursement_request",
                        agent_state="closed_without_execution", request_outcome="zero_reimbursement",
                        reason=f"The reimbursement is EUR 0.00 because {cause}.", calculation=calculation)

    # 8. Approval needed? Only when the FINAL amount is strictly above the threshold.
    if requires_authorization(calculation.final, threshold):
        return Decision(action="request_authorization", request_category="reimbursement_request",
                        agent_state="awaiting_authorization", request_outcome="authorization_requested",
                        reason=f"Final reimbursement EUR {money_str(calculation.final)} exceeds EUR {threshold}; "
                               "insurer authorization is required before payment.",
                        calculation=calculation, authorization_threshold=threshold)
    return Decision(action="pay", request_category="reimbursement_request", agent_state="completed",
                    request_outcome="paid", reason=f"Eligible: final reimbursement EUR {money_str(calculation.final)} "
                    f"is within the EUR {threshold} autonomous threshold.",
                    calculation=calculation, authorization_threshold=threshold)


# --- Converting untrusted extracted values into typed facts -------------------------------------

_SERVICES = {"specialist_consultation", "diagnostic_test", "physiotherapy",
             "dental_care", "cosmetic_treatment", "medicines", "other"}
_AMOUNT = re.compile(r"^\d+(\.\d{1,2})?$")


def parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()) if value else None
    except ValueError:
        return None


def parse_amount(value) -> Decimal | None:
    text = str(value or "").replace(",", "").replace(" ", "").removeprefix("EUR").removeprefix("€")
    if not _AMOUNT.match(text):
        return None
    try:
        return round_half_up(Decimal(text))
    except InvalidOperation:
        return None


def _clean(value):
    value = " ".join(str(value).split()) if value is not None else ""
    return value or None


def _grounded(value, source):
    """Identifiers must literally appear in the document; otherwise treat them as not supplied."""
    value = _clean(value)
    return value if value and value.casefold() in (source or "").casefold() else None


def facts_from_extraction(extraction: dict, request: dict) -> ClaimFacts:
    """Typed facts from model output. Fields from absent documents are discarded, never trusted."""
    invoice_text = (request.get("invoice_text") or "").strip()
    prescription_text = (request.get("prescription_text") or "").strip()
    service = extraction.get("service_category")
    service = service if service in _SERVICES else None
    facts = {"service_category": service,
             "multiple_services_or_patients": bool(extraction.get("multiple_services_or_patients"))}
    if invoice_text:
        currency = _clean(extraction.get("currency"))
        facts.update(
            has_invoice=True,
            provider_id=_grounded(extraction.get("provider_id"), invoice_text),
            invoice_number=_grounded(extraction.get("invoice_number"), invoice_text),
            invoice_date=parse_date(extraction.get("invoice_date")),
            service_date=parse_date(extraction.get("service_date")),
            patient_name=_clean(extraction.get("patient_name")),
            service_description=_clean(extraction.get("service_description")),
            expense=parse_amount(extraction.get("amount")),
            currency=currency.upper() if currency else None,
            paid=extraction.get("paid") if isinstance(extraction.get("paid"), bool) else None,
        )
    if prescription_text:
        prescribed = extraction.get("prescription_service_category")
        facts.update(has_prescription=True,
                     prescription_patient_name=_clean(extraction.get("prescription_patient_name")),
                     prescription_service_category=prescribed if prescribed in _SERVICES else None)
    return ClaimFacts(**facts)
