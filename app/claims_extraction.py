"""Datapizza-ai extraction of reimbursement facts from invoice/prescription text.

The model only transcribes facts (structured output, no tools, one step). It decides nothing:
app/claims_engine.py validates every value again and takes all decisions.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.skills import load_skill

EXTRACT_ATTEMPTS = 2

ServiceCategory = Literal["specialist_consultation", "diagnostic_test", "physiotherapy",
                          "dental_care", "cosmetic_treatment", "medicines", "other"]


class ClaimExtraction(BaseModel):
    """Flat on purpose: one level of optional fields is the most reliable structured-output shape."""

    service_category: ServiceCategory | None = Field(
        None, description="Category of the service to reimburse (invoice service, else the service named in the message).")
    multiple_services_or_patients: bool = Field(
        False, description="True only if the request asks to reimburse several distinct services or patients.")
    provider_id: str | None = Field(None, description="Provider ID exactly as written on the invoice.")
    invoice_number: str | None = Field(None, description="Invoice number exactly as written on the invoice.")
    invoice_date: str | None = Field(None, description="Invoice date, YYYY-MM-DD.")
    service_date: str | None = Field(None, description="Date the service was performed, YYYY-MM-DD.")
    patient_name: str | None = Field(None, description="Patient named on the invoice.")
    service_description: str | None = Field(None, description="Service description as written on the invoice.")
    amount: str | None = Field(None, description="Invoice total as a plain decimal, e.g. 180.00.")
    currency: str | None = Field(None, description="ISO currency code of the amount, e.g. EUR.")
    paid: bool | None = Field(None, description="True if the invoice states it is paid, false if unpaid, null if not stated.")
    prescription_patient_name: str | None = Field(None, description="Patient named on the prescription.")
    prescription_service_category: ServiceCategory | None = Field(
        None, description="Category of the service the prescription prescribes.")


class ExtractionUnavailable(RuntimeError):
    """The extraction model failed on every attempt."""


def system_prompt():
    return ("You extract facts from a medical reimbursement request for an insurer. Transcribe what the "
            "documents say; do not decide eligibility, amounts or approvals. Return null for anything "
            "not stated.\n\n" + load_skill("claims", "reimbursement"))


def extraction_input(request):
    fields = ("text", "invoice_text", "prescription_text")
    return json.dumps({key: request.get(key) for key in fields}, ensure_ascii=False)


async def extract_claim(request, client) -> ClaimExtraction:
    from datapizza.agents import Agent

    failures = []
    for _ in range(EXTRACT_ATTEMPTS):
        agent = Agent(name="claims_extractor", client=client, system_prompt=system_prompt(),
                      output_cls=ClaimExtraction, max_steps=1)
        try:
            step = await agent.a_run(extraction_input(request))
            parsed = step.structured_data[0] if step and step.structured_data else None
            return parsed if isinstance(parsed, ClaimExtraction) else ClaimExtraction.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced as ExtractionUnavailable
            failures.append(type(exc).__name__)
    raise ExtractionUnavailable(f"Claim extraction failed {EXTRACT_ATTEMPTS} times: {', '.join(failures)}")
