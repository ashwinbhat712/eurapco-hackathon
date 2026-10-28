"""Offline tests of the live agent's model-facing pieces (no model calls)."""

import asyncio
from types import SimpleNamespace

from app.live.agent import LlmAgent
from app.live.extraction import AdminExtraction, ClaimsExtraction, to_findings
from app.workflow.ports import Call
from app.workflow.states import Agent


def test_findings_keep_only_known_found_facts():
    findings = to_findings(AdminExtraction(category="contact_update", phone="+393330000000", question="why?"))
    assert findings.facts == {"phone": "+393330000000"}


def test_closing_category_carries_no_facts():
    findings = to_findings(ClaimsExtraction(category="unclear", claim_id="CL-1", other_requests=["renew"]))
    assert findings.facts == {} and findings.other_requests == ("renew",)


class FakeClient:
    def __init__(self, args_json):
        self.args_json, self.calls = args_json, 0

    async def a_structured_response(self, input, output_cls, system_prompt):
        self.calls += 1
        return SimpleNamespace(structured_data=[output_cls(args_json=self.args_json)])


def test_first_proposal_is_the_rule_call_without_a_model_call():
    client = FakeClient("{}")
    expected = Call(tool="pay_claim", args={"policy_id": "POL-001", "claim_id": "CL-1", "amount": "1.00"})
    assert asyncio.run(LlmAgent(Agent.CLAIMS, client).propose_write(None, expected, None, None)) == expected
    assert client.calls == 0


def test_correction_comes_from_the_model():
    client = FakeClient('{"policy_id": "POL-001", "changes": {"phone": "+393330000000"}}')
    rejected = Call(tool="update_contact_details", args={"policy_id": "POL-001", "changes": {"phone": "+39 333"}})
    error = {"code": "INVALID_ARGUMENT", "field": "changes.phone"}
    corrected = asyncio.run(LlmAgent(Agent.ADMINISTRATION, client).propose_write(None, rejected, error, None))
    assert corrected.args["changes"]["phone"] == "+393330000000"
