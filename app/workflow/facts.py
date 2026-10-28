"""Facts per request category: which keys an agent may gather for each operation (docs/minimum-information.md).

Every field is optional: gathering is partial, and `None` means "still missing". Values stay as the customer
gave them (strings); formats and catalog values are checked later by the rules and tools.schema.json, so an
unsupported value (e.g. a 7000.00 limit) is a business answer, not a schema error.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.workflow.schema import Vocabulary


class Facts(BaseModel):
    """Closed key set: a fact the category does not know is refused."""

    model_config = ConfigDict(extra="forbid")


class NoFacts(Facts):
    """Closing and reroute categories (out_of_scope_*, unclear) carry no facts; anything sent is dropped."""

    model_config = ConfigDict(extra="ignore")


class PolicyEnquiryFacts(Facts):
    question: str | None = None


class ContactUpdateFacts(Facts):
    """Only the fields to change. A replaced address needs all four address parts."""

    email: str | None = None
    phone: str | None = None
    street: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country: str | None = None


class CoverageChangeFacts(Facts):
    """The intended final values, not the difference."""

    annual_limit: str | None = None
    deductible: str | None = None


class PolicyTerminationFacts(Facts):
    explicit_instruction: bool | None = None


class ClaimEnquiryFacts(Facts):
    claim_id: str | None = None
    question: str | None = None


class ReimbursementFacts(Facts):
    """Invoice and, for diagnostic tests and physiotherapy, prescription contents."""

    patient_name: str | None = None
    service_category: str | None = None
    service_description: str | None = None
    provider_id: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    service_date: str | None = None
    expense: str | None = None
    currency: str | None = None
    invoice_paid: bool | None = None
    prescription_patient: str | None = None
    prescription_service: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


FACTS_BY_CATEGORY: dict[str, type[Facts]] = {
    **{category: NoFacts for category in Vocabulary.CATEGORIES},
    "policy_enquiry": PolicyEnquiryFacts,
    "contact_update": ContactUpdateFacts,
    "coverage_change": CoverageChangeFacts,
    "policy_termination": PolicyTerminationFacts,
    "claim_enquiry": ClaimEnquiryFacts,
    "reimbursement_request": ReimbursementFacts,
}


def facts_model(category):
    return FACTS_BY_CATEGORY[category]
