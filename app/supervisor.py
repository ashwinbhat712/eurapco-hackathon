"""Supervisor: classifies each request and decides which subagent handles it.

The model only classifies (structured output, no tools). Agent selection, vocabulary
checks and re-routing are deterministic code, so the log always gets one coherent
handling_agent/request_category pair. There is no keyword fallback: if classification
keeps failing, the request fails as a technical failure.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.contracts import ROOT, document

CLASSIFY_ATTEMPTS = 2

AGENTS = ("administration", "claims")
OTHER_AGENT = {"administration": "claims", "claims": "administration"}
# Category a subagent returns when the operation belongs to the other agent.
CROSS_DOMAIN = {"administration": "out_of_scope_claims", "claims": "out_of_scope_administration"}

SupervisorCategory = Literal[
    "policy_enquiry", "contact_update", "coverage_change", "policy_termination",
    "out_of_scope_iban", "out_of_scope_renewal",
    "reimbursement_request", "claim_enquiry", "out_of_scope_claim_integration",
    "out_of_scope_other", "unclear",
]


class Classification(BaseModel):
    request_category: SupervisorCategory = Field(description="The single requested operation.")
    agent: Literal["administration", "claims"] = Field(
        description="Agent that owns the operation; decisive only for out_of_scope_other and unclear.")
    rationale: str = Field(description="One short sentence explaining the classification.")


class RoutingDecision(BaseModel):
    """Validated supervisor decision written to the run log: admin/claims routing plus its rationale."""

    agent: Literal["administration", "claims"] = Field(description="Agent that will handle the request.")
    request_category: SupervisorCategory = Field(description="Category assigned by the supervisor.")
    rationale: str = Field(min_length=1, description="Why the supervisor chose this classification.")
    attempts: int = Field(default=1, ge=1, description="Classification attempts used.")

    def as_dict(self):
        return self.model_dump()


class SupervisorUnavailable(RuntimeError):
    """The supervisor could not classify the request after all attempts."""


def categories(agent):
    return document("vocabulary.json")["request_categories"][agent]


def owner_of(category):
    """Agent implied by a category, or None when both agents share it."""
    owners = [agent for agent in AGENTS if category in categories(agent)]
    return owners[0] if len(owners) == 1 else None


def normalize(classification: Classification, attempts=1) -> RoutingDecision:
    category = classification.request_category
    agent = owner_of(category) or classification.agent
    return RoutingDecision(agent=agent, request_category=category,
                           rationale=classification.rationale, attempts=attempts)


def supervisor_input(request):
    fields = ("text", "claim_id", "invoice_text", "prescription_text")
    return json.dumps({key: request.get(key) for key in fields}, ensure_ascii=False)


def system_prompt():
    return (ROOT / "agents" / "supervisor" / "prompt.md").read_text(encoding="utf-8")


async def classify(request, client) -> RoutingDecision:
    """Structured LLM classification, retried; raises SupervisorUnavailable when every attempt fails."""
    from datapizza.agents import Agent

    failures = []
    for attempt in range(1, CLASSIFY_ATTEMPTS + 1):
        supervisor = Agent(name="supervisor", client=client, system_prompt=system_prompt(),
                           output_cls=Classification, max_steps=1)
        try:
            step = await supervisor.a_run(supervisor_input(request))
            parsed = step.structured_data[0] if step and step.structured_data else None
            if not isinstance(parsed, Classification):
                parsed = Classification.model_validate(parsed)
            return normalize(parsed, attempt)
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced as SupervisorUnavailable
            failures.append(type(exc).__name__)
    raise SupervisorUnavailable(f"Supervisor classification failed {CLASSIFY_ATTEMPTS} times: {', '.join(failures)}")
