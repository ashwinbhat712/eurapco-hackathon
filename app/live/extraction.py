"""Structured output the model fills while gathering; turned into Findings for the harness.

Every property is required and none is nullable: "" / false means "not found". Optional and union-typed
properties make the API's grammar compilation exponentially slower (platform.claude.com, structured outputs,
"Schema complexity limits"); the claims schema with 14 nullable fields timed out on the first call.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.workflow.facts import facts_model
from app.workflow.ports import Findings
from app.workflow.schema import Vocabulary
from app.workflow.states import Agent

AdminCategory = Literal[tuple(sorted(Vocabulary.BY_AGENT[Agent.ADMINISTRATION]))]
ClaimsCategory = Literal[tuple(sorted(Vocabulary.BY_AGENT[Agent.CLAIMS]))]
ServiceCategory = Literal["", "specialist_consultation", "diagnostic_test", "physiotherapy",
                          "dental_care", "cosmetic_treatment", "medicines", "other"]
NOT_FOUND = (None, "", False)


def _all_required(schema):
    schema["required"] = list(schema.get("properties", {}))


class Extraction(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_required)

    other_requests: list[str] = Field(default_factory=list, description="Other operations asked for, not handled.")
    withdrawn: bool = Field(default=False, description="True only if the customer takes the request back.")


class AdminExtraction(Extraction):
    category: AdminCategory
    question: str = ""
    email: str = ""
    phone: str = ""
    street: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = ""
    annual_limit: str = Field(default="", description="Requested final value, e.g. 5000.00.")
    deductible: str = Field(default="", description="Requested final value, e.g. 200.00.")
    explicit_instruction: bool = Field(default=False, description="True if told to terminate now.")


class ClaimsExtraction(Extraction):
    category: ClaimsCategory
    claim_id: str = ""
    question: str = ""
    patient_name: str = Field(default="", description="Patient named on the invoice.")
    service_category: ServiceCategory = ""
    service_description: str = ""
    provider_id: str = ""
    invoice_number: str = ""
    invoice_date: str = Field(default="", description="YYYY-MM-DD.")
    service_date: str = Field(default="", description="YYYY-MM-DD.")
    expense: str = Field(default="", description="Invoice total, e.g. 180.00.")
    currency: str = ""
    invoice_paid: bool = Field(default=False, description="True only if the invoice says it is paid.")
    prescription_patient: str = ""
    prescription_service: str = ""


EXTRACTION_BY_AGENT = {Agent.ADMINISTRATION: AdminExtraction, Agent.CLAIMS: ClaimsExtraction}


def to_findings(extraction):
    """Keep only the facts the chosen category knows, and only those actually found."""
    category = extraction.category
    values = extraction.model_dump()
    facts = {name: values[name] for name in facts_model(category).model_fields if values.get(name) not in NOT_FOUND}
    return Findings(category=category, facts=facts, other_requests=tuple(extraction.other_requests),
                    withdrawn=extraction.withdrawn)
