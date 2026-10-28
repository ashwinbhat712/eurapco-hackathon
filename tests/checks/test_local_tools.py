"""The local check tools: bound policy, fresh reads, error envelopes, datapizza wrapping and logging."""

import asyncio
import json

import pytest

from app.checks.cache import ReadCache
from app.checks.toolkit import LocalToolkit
from app.checks.tools import CalculateReimbursement, CheckCoverageChange, CheckReimbursementEligibility, \
    FindDuplicateClaim
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


@pytest.fixture
def reader(domain):
    return ReadCache(domain)


def run(coroutine):
    return asyncio.run(coroutine)


def test_calculation_reads_current_policy(domain, reader):
    result = run(CalculateReimbursement(reader, "POL-002")(expense="EUR 2,000.00"))
    assert result["data"]["final"] == "900.00" and result["data"]["capped_by_remaining_limit"] is True
    assert domain.calls == [("get_policy", {"policy_id": "POL-002"})]


def test_calculation_sees_a_change_made_earlier_in_the_run(reader):
    run(reader.call("update_coverage", {"policy_id": "POL-001", "changes": {"deductible": "200.00"}}))
    assert run(CalculateReimbursement(reader, "POL-001")(expense="180"))["data"]["final"] == "0.00"


def test_bad_amount_is_a_correctable_error_without_reads(domain, reader):
    result = run(CalculateReimbursement(reader, "POL-001")(expense="1.925"))
    assert result["ok"] is False and result["error"]["field"] == "expense" and result["error"]["retryable"]
    assert domain.calls == []


def test_unknown_policy_returns_the_domain_error(reader):
    result = run(CheckCoverageChange(reader, "POL-999")(annual_limit="5000"))
    assert result["ok"] is False and result["error"]["code"] == "NOT_FOUND"


def test_duplicate_tool_finds_history(reader):
    result = run(FindDuplicateClaim(reader, "POL-001")(provider_id="PROVIDER-001", invoice_number="INV-2026-001",
                                                       invoice_date="2026-02-02"))
    assert result["data"]["duplicate"] is True and result["data"]["existing_claim_id"] == "CL-H001"


def test_duplicate_tool_refuses_an_unreadable_date_instead_of_saying_no_duplicate(domain, reader):
    result = run(FindDuplicateClaim(reader, "POL-001")(provider_id="PROVIDER-001", invoice_number="INV-2026-001",
                                                       invoice_date="02/02/2026"))
    assert result["ok"] is False and result["error"]["field"] == "invoice_date" and domain.calls == []


def test_eligibility_tool(reader):
    result = run(CheckReimbursementEligibility(reader, "POL-003")(
        patient_name="Jamie Demo", service_category="physiotherapy", service_date="2026-03-01",
        invoice_date="2026-03-02", expense="100.00"))
    assert result["data"]["outcome"] == "inactive_policy"


def test_coverage_tool_needs_at_least_one_value(domain, reader):
    result = run(CheckCoverageChange(reader, "POL-001")())
    assert result["ok"] is False and result["error"]["code"] == "INVALID_ARGUMENT" and domain.calls == []


def test_all_missing_arguments_are_named(reader):
    result = run(FindDuplicateClaim(reader, "POL-001")(provider_id="PROVIDER-001", invoice_number=""))
    error = result["error"]
    assert error["field"] == "invoice_number" and "invoice_number, invoice_date" in error["message"]


class TestReadCache:
    def test_model_read_is_reused_by_every_local_check(self, domain, reader):
        run(reader.call("get_policy", {"policy_id": "POL-002"}))  # The model's own read.
        run(CalculateReimbursement(reader, "POL-002")(expense="100.00"))
        run(CheckCoverageChange(reader, "POL-002")(annual_limit="5000.00"))
        assert domain.calls == [("get_policy", {"policy_id": "POL-002"})]

    def test_a_write_clears_the_cache(self, domain, reader):
        tool = CalculateReimbursement(reader, "POL-001")
        assert run(tool(expense="180"))["data"]["final"] == "104.00"
        run(reader.call("update_coverage", {"policy_id": "POL-001", "changes": {"deductible": "200.00"}}))
        assert run(tool(expense="180"))["data"]["final"] == "0.00"
        assert [name for name, _ in domain.calls] == ["get_policy", "update_coverage", "get_policy"]

    def test_a_failed_write_also_clears_the_cache(self, domain, reader):
        run(reader.call("get_policy", {"policy_id": "POL-001"}))
        run(reader.call("pay_claim", {"policy_id": "POL-001", "claim_id": "CL-NOPE", "amount": "1.00"}))
        run(reader.read("get_policy", {"policy_id": "POL-001"}))
        assert [name for name, _ in domain.calls] == ["get_policy", "pay_claim", "get_policy"]

    def test_a_failed_read_is_not_cached(self, domain, reader):
        run(reader.read("get_policy", {"policy_id": "POL-999"}))
        run(reader.read("get_policy", {"policy_id": "POL-999"}))
        assert len(domain.calls) == 2

    def test_other_policy_or_tool_is_another_entry(self, domain, reader):
        run(reader.read("get_policy", {"policy_id": "POL-001"}))
        run(reader.read("get_policy", {"policy_id": "POL-002"}))
        run(reader.read("list_claims", {"policy_id": "POL-001"}))
        assert len(domain.calls) == 3


class BrokenDomain(Domain):
    """Returns a policy whose terms cannot be read."""

    async def call(self, name, arguments):
        result = await super().call(name, arguments)
        result["data"]["financial_terms"]["deductible"] = "fifty"
        return result


def test_unreadable_domain_data_is_an_error_not_a_crash(tmp_path):
    store = DomainStore(tmp_path)
    result = run(CalculateReimbursement(ReadCache(BrokenDomain(store, store.start_run())), "POL-001")(
        expense="180.00"))
    assert result["ok"] is False and result["error"]["code"] == "OPERATION_FAILED"
    assert result["error"]["retryable"] is False


@pytest.mark.parametrize("agent", ["administration", "claims"])
def test_datapizza_tools_per_agent(reader, agent):
    names = [tool.name for tool in LocalToolkit(agent, reader, "POL-001").datapizza_tools()]
    assert names == [tool.name for tool in LocalToolkit.ACCESS[agent]]


def test_model_cannot_choose_the_policy_and_calls_are_logged(reader):
    log = Log()
    tool = next(tool for tool in LocalToolkit("claims", reader, "POL-002", log).datapizza_tools()
                if tool.name == "calculate_reimbursement")
    result = json.loads(run(tool.func(expense="2000.00", policy_id="POL-001")))
    assert result["data"]["final"] == "900.00"
    assert [kind for kind, _ in log.events] == ["tool_call", "tool_result"]
    assert log.events[0][1] == {"name": "calculate_reimbursement", "arguments": {"expense": "2000.00"},
                                "ignored": ["policy_id"], "local": True}


def test_null_optional_argument_falls_back_to_default(reader):
    tool = next(tool for tool in LocalToolkit("claims", reader, "POL-001").datapizza_tools()
                if tool.name == "check_reimbursement_eligibility")
    result = json.loads(run(tool.func(patient_name="Jamie Example", service_category="physiotherapy",
                                      service_date="2026-03-01", invoice_date="2026-03-02", expense="100.00",
                                      currency=None)))
    assert result["data"]["ok"] is True


def test_guidance_explains_tool_errors():
    assert "retryable" in LocalToolkit.guidance("claims") and "retryable" in LocalToolkit.guidance("administration")
