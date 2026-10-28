"""Supervisor routing: normalization, fallback and one-hop re-routing, without a model."""

import asyncio
import json

import pytest
from pydantic import ValidationError

import app.runtime as runtime
from app.contracts import ROOT
from app.evaluation import EvaluationLog
from app.subagents import parse_response
from app.supervisor import Classification, RoutingDecision, SupervisorUnavailable, classify, normalize


def ticket(name="T01"):
    return json.loads((ROOT / f"examples/training/{name}.json").read_text())


def final(category, state="completed", outcome="answered"):
    return {"request_category": category, "agent_state": state, "request_outcome": outcome,
            "reason": "r", "message": "m", "question": None, "proposal": None}


@pytest.mark.parametrize("category,chosen,expected", [
    ("reimbursement_request", "administration", "claims"),
    ("coverage_change", "claims", "administration"),
    ("out_of_scope_other", "claims", "claims"),
    ("unclear", "administration", "administration"),
])
def test_category_decides_agent_unless_shared(category, chosen, expected):
    decision = normalize(Classification(request_category=category, agent=chosen, rationale="x"))
    assert (decision.agent, decision.request_category) == (expected, category)


class FlakyClient:
    """Fails the first `failures` structured calls, then classifies as claim_enquiry."""

    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    async def a_structured_response(self, **_):
        from datapizza.core.clients import ClientResponse
        from datapizza.type import StructuredBlock

        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("provider down")
        parsed = Classification(request_category="claim_enquiry", agent="claims", rationale="x")
        return ClientResponse(content=[StructuredBlock(content=parsed)])


def test_supervisor_retries_before_succeeding():
    client = FlakyClient(failures=1)
    decision = asyncio.run(classify(ticket("T02"), client))
    assert (decision.agent, decision.request_category, decision.attempts) == ("claims", "claim_enquiry", 2)


def test_supervisor_fails_without_guessing_after_retries():
    client = FlakyClient(failures=99)
    with pytest.raises(SupervisorUnavailable, match="RuntimeError"):
        asyncio.run(classify(ticket("T08"), client))
    assert client.calls == 2


def test_parse_response_drops_echoed_schema_keys():
    text = json.dumps({"$schema": "x", **final("contact_update", outcome="updated")})
    assert "$schema" not in parse_response(f"```json\n{text}\n```")


class FakeDomain:
    def __init__(self, run_id, agent, log):
        self.agent = agent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def scripted(answers, writes=()):
    calls = []

    class FakeSubagent:
        def __init__(self, agent, client, domain):
            self.agent, self.successful_writes = agent, list(writes)

        async def run(self, request, hint=None):
            calls.append((self.agent, hint))
            return answers[self.agent]

    return FakeSubagent, calls


def run_dispatch(monkeypatch, tmp_path, decision, answers, writes=()):
    fake, calls = scripted(answers, writes)
    monkeypatch.setattr(runtime, "DomainClient", FakeDomain)
    monkeypatch.setattr(runtime, "SubagentRun", fake)
    log = EvaluationLog(ticket(), "RUN-test", "TEAM-1", tmp_path)
    result = asyncio.run(runtime.dispatch(ticket(), "RUN-test", log, None, decision))
    return result, calls, log


def decision_for(agent, category):
    return RoutingDecision(agent=agent, request_category=category, rationale="x")


def test_routing_decision_is_validated_for_logging():
    decision = decision_for("claims", "claim_enquiry")
    assert decision.as_dict() == {"agent": "claims", "request_category": "claim_enquiry",
                                  "rationale": "x", "attempts": 1}
    with pytest.raises(ValidationError):
        RoutingDecision(agent="billing", request_category="claim_enquiry", rationale="x")
    with pytest.raises(ValidationError):
        RoutingDecision(agent="claims", request_category="claim_enquiry", rationale="")


def test_cross_domain_answer_is_rerouted_once(monkeypatch, tmp_path):
    decision = decision_for("administration", "claim_enquiry")
    answers = {"administration": final("out_of_scope_claims", "closed_without_execution", "out_of_scope"),
               "claims": final("claim_enquiry")}
    result, calls, log = run_dispatch(monkeypatch, tmp_path, decision, answers)
    assert [agent for agent, _ in calls] == ["administration", "claims"]
    assert result["request_category"] == "claim_enquiry"
    assert log.record["handling_agent"] == "claims"
    routes = [event["data"] for event in log.record["events"] if event["kind"] == "routing"]
    assert routes[0] == {"handling_agent": "administration", "source": "supervisor",
                         "request_category": "claim_enquiry", "rationale": "x", "attempts": 1}
    assert routes[1] == {"handling_agent": "claims", "source": "administration",
                         "request_category": "out_of_scope_claims", "rationale": "r"}


def test_no_reroute_after_successful_write(monkeypatch, tmp_path):
    decision = decision_for("administration", "contact_update")
    answers = {"administration": final("out_of_scope_claims", "closed_without_execution", "out_of_scope")}
    _, calls, _ = run_dispatch(monkeypatch, tmp_path, decision, answers, writes=["update_contact_details"])
    assert [agent for agent, _ in calls] == ["administration"]


def test_supervisor_category_is_passed_as_hint(monkeypatch, tmp_path):
    decision = decision_for("claims", "claim_enquiry")
    _, calls, _ = run_dispatch(monkeypatch, tmp_path, decision, {"claims": final("claim_enquiry")})
    assert "request_category=claim_enquiry" in calls[0][1]
