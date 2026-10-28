"""The local check tools: bound policy, fresh reads, error envelopes, datapizza wrapping and logging."""

import asyncio
import json

import pytest

from app.checks.tools import LOCAL_ACCESS, LocalTools, datapizza_tools
from app.domain import DomainStore


class Domain:
    """DomainClient stand-in over the real in-process domain; records what was read."""

    def __init__(self, store, run_id):
        self.store, self.run_id, self.calls = store, run_id, []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.store.call(name, {"run_id": self.run_id, **arguments})


class Log:
    def __init__(self):
        self.events = []

    def event(self, kind, **data):
        self.events.append((kind, data))


@pytest.fixture
def domain(tmp_path):
    store = DomainStore(tmp_path)
    return Domain(store, store.start_run())


def run(coroutine):
    return asyncio.run(coroutine)


def test_calculation_reads_current_policy(domain):
    result = run(LocalTools(domain, "POL-002").calculate_reimbursement("EUR 2,000.00"))
    assert result["data"]["final"] == "900.00" and result["data"]["capped_by_remaining_limit"] is True
    assert domain.calls == [("get_policy", {"policy_id": "POL-002"})]


def test_calculation_sees_a_change_made_earlier_in_the_run(domain):
    run(domain.call("update_coverage", {"policy_id": "POL-001", "changes": {"deductible": "200.00"}}))
    assert run(LocalTools(domain, "POL-001").calculate_reimbursement("180"))["data"]["final"] == "0.00"


def test_bad_amount_is_a_correctable_error_without_reads(domain):
    result = run(LocalTools(domain, "POL-001").calculate_reimbursement("1.925"))
    assert result["ok"] is False and result["error"]["field"] == "expense" and domain.calls == []


def test_unknown_policy_returns_the_domain_error(domain):
    result = run(LocalTools(domain, "POL-999").check_coverage_change(annual_limit="5000"))
    assert result["ok"] is False and result["error"]["code"] == "NOT_FOUND"


def test_duplicate_tool_finds_history(domain):
    result = run(LocalTools(domain, "POL-001").find_duplicate_claim("PROVIDER-001", "INV-2026-001", "2026-02-02"))
    assert result["data"]["duplicate"] is True and result["data"]["existing_claim_id"] == "CL-H001"


def test_eligibility_tool(domain):
    result = run(LocalTools(domain, "POL-003").check_reimbursement_eligibility(
        "Jamie Demo", "physiotherapy", "2026-03-01", "2026-03-02", "100.00"))
    assert result["data"]["outcome"] == "inactive_policy"


@pytest.mark.parametrize("agent", ["administration", "claims"])
def test_datapizza_tools_per_agent(domain, agent):
    assert [tool.name for tool in datapizza_tools(agent, domain, "POL-001")] == list(LOCAL_ACCESS[agent])


def test_model_cannot_choose_the_policy_and_calls_are_logged(domain):
    log = Log()
    tool = next(tool for tool in datapizza_tools("claims", domain, "POL-002", log)
                if tool.name == "calculate_reimbursement")
    result = json.loads(run(tool.func(expense="2000.00", policy_id="POL-001")))
    assert result["data"]["final"] == "900.00"
    assert [kind for kind, _ in log.events] == ["tool_call", "tool_result"]
    assert log.events[0][1] == {"name": "calculate_reimbursement", "arguments": {"expense": "2000.00"}, "local": True}


def test_missing_required_argument_is_an_error(domain):
    tool = datapizza_tools("claims", domain, "POL-001")[2]
    result = json.loads(run(tool.func(provider_id="PROVIDER-001", invoice_number="")))
    assert result["ok"] is False and result["error"]["field"] == "invoice_number"
