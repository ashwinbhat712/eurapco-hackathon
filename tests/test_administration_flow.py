"""Flow tests: the administration executor against the real domain store and logger.

No model and no MCP server: the extractor is a stub and the domain tools are called
directly, so the assertions are about actual effects in the domain snapshot.
"""

import asyncio
import copy
import json

import pytest

from agents.administration.agent import _execute, correct_arguments, handle
from agents.administration.extraction import AdminIntent
from agents.administration.ports import InMemoryProposalStore, WorkflowState, build_proposal
from agents.administration.rules import Decision
from app.domain import DomainStore
from app.evaluation import EvaluationLog

REQUEST = {"event_type": "request", "event_id": "EV-TF-01", "ticket_id": "TF", "policy_id": "POL-001",
           "author_name": "Taylor Example", "timestamp": "2026-10-22T10:00:00+02:00", "text": "test"}


class LocalDomain:
    """Same envelope as DomainClient, without MCP transport."""

    def __init__(self, store, run_id, log, forced_error=None):
        self.store, self.run_id, self.log, self.forced_error = store, run_id, log, forced_error
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        self.log.event("tool_call", name=name, arguments={"run_id": self.run_id, **arguments})
        if self.forced_error and name != "get_policy":
            result = {"ok": False, "error": dict(self.forced_error)}
        else:
            result = self.store.call(name, {**arguments, "run_id": self.run_id})
        self.log.event("tool_result", name=name, result=result)
        return result


class StubExtractor:
    def __init__(self, *payloads):
        self.payloads = list(payloads)

    async def extract(self, event, workflow):
        return AdminIntent.from_payload(self.payloads.pop(0))


@pytest.fixture
def harness(tmp_path):
    store = DomainStore(directory=tmp_path / "domain")
    run_id = store.start_run("RUN-TEST")
    log = EvaluationLog(REQUEST, run_id, "TEAM-TEST", directory=tmp_path / "logs")
    workflow = WorkflowState(ticket_id=REQUEST["ticket_id"], run_id=run_id, policy_id="POL-001",
                             facts={"request": REQUEST})
    return store, run_id, log, workflow, InMemoryProposalStore()


def policy_of(store, run_id):
    return store.snapshot(run_id)["policies"]["POL-001"]


def test_contact_update_applies_and_reports_the_actual_values(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    extractor = StubExtractor({"request_category": "contact_update", "contact_fields": ["phone"],
                               "phone": "+393339998888", "summary": "phone change"})

    final = asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))

    assert (final["agent_state"], final["request_outcome"]) == ("completed", "updated")
    assert "+393331234567" in final["message"] and "+393339998888" in final["message"]
    assert policy_of(store, run_id)["contacts"]["phone"] == "+393339998888"
    assert log.record["handling_agent"] == "administration"
    assert log.record["request_category"] == "contact_update"


def test_no_change_does_not_call_the_write_tool(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    extractor = StubExtractor({"request_category": "contact_update", "contact_fields": ["phone"],
                               "phone": "+393331234567", "summary": "same phone"})

    final = asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))

    assert final["request_outcome"] == "no_change"
    assert [name for name, _ in domain.calls] == ["get_policy"]


def test_clarification_then_reply_completes_the_same_workflow(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    extractor = StubExtractor(
        {"request_category": "contact_update", "contact_fields": ["address"],
         "address": {"street": "8 Orchard Road", "city": "Turin", "country": "Italy"}, "summary": "address"},
        {"request_category": "contact_update", "contact_fields": ["address"],
         "address": {"street": "8 Orchard Road", "city": "Turin", "postal_code": "10100", "country": "Italy"},
         "summary": "address with postal code"})

    waiting = asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))
    assert waiting["agent_state"] == "awaiting_clarification"
    assert waiting["question"] and workflow.clarification_asked
    assert policy_of(store, run_id)["contacts"]["address"]["city"] == "Milan"

    reply = {"event_type": "clarification_reply", "event_id": "EV-TF-02", "ticket_id": "TF",
             "timestamp": "2026-10-22T10:01:00+02:00", "text": "The postal code is 10100."}
    workflow.phase = "clarification_reply"
    log.input(reply)
    final = asyncio.run(handle(reply, workflow, domain, proposals, log, extractor))

    assert (final["agent_state"], final["request_outcome"]) == ("completed", "updated")
    assert policy_of(store, run_id)["contacts"]["address"] == {
        "street": "8 Orchard Road", "city": "Turin", "postal_code": "10100", "country": "Italy"}
    assert log.record["human_rounds"] == {"clarification": 1, "authorization": 0}


def test_coverage_proposal_waits_then_approval_executes_exactly_it(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    extractor = StubExtractor({"request_category": "coverage_change", "annual_limit": "10000",
                               "deductible": "500", "summary": "combined increase"})

    waiting = asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))
    assert (waiting["agent_state"], waiting["request_outcome"]) == ("awaiting_authorization", "authorization_requested")
    assert waiting["proposal"]["hash"] == proposals.load("TF")["hash"]
    assert policy_of(store, run_id)["financial_terms"]["annual_limit"] == "3000.00"

    decision = {"event_type": "insurer_decision", "event_id": "EV-TF-02", "ticket_id": "TF",
                "timestamp": "2026-10-22T10:02:00+02:00", "decision": "approve"}
    workflow.phase = "insurer_decision"
    log.input(decision)
    final = asyncio.run(handle(decision, workflow, domain, proposals, log, extractor))

    assert (final["agent_state"], final["request_outcome"]) == ("completed", "updated")
    terms = policy_of(store, run_id)["financial_terms"]
    assert (terms["annual_limit"], terms["deductible"]) == ("10000.00", "500.00")
    assert log.record["human_rounds"] == {"clarification": 0, "authorization": 1}


def test_insurer_rejection_writes_nothing(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    extractor = StubExtractor({"request_category": "policy_termination", "explicit_termination": True,
                               "summary": "terminate"})

    waiting = asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))
    assert waiting["agent_state"] == "awaiting_authorization"

    decision = {"event_type": "insurer_decision", "event_id": "EV-TF-02", "ticket_id": "TF",
                "timestamp": "2026-10-22T10:02:00+02:00", "decision": "reject",
                "reason": "The proposal is declined for this training execution."}
    workflow.phase = "insurer_decision"
    final = asyncio.run(handle(decision, workflow, domain, proposals, log, extractor))

    assert (final["agent_state"], final["request_outcome"]) == ("closed_without_execution", "insurer_rejected")
    assert policy_of(store, run_id)["status"] == "active"
    assert "terminate_policy" not in [name for name, _ in domain.calls]


def test_approved_termination_terminates_the_policy(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    extractor = StubExtractor({"request_category": "policy_termination", "explicit_termination": True,
                               "summary": "terminate"})
    asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))

    decision = {"event_type": "insurer_decision", "event_id": "EV-TF-02", "ticket_id": "TF",
                "timestamp": "2026-10-22T10:02:00+02:00", "decision": "approve"}
    workflow.phase = "insurer_decision"
    final = asyncio.run(handle(decision, workflow, domain, proposals, log, extractor))

    assert (final["agent_state"], final["request_outcome"]) == ("completed", "terminated")
    assert policy_of(store, run_id)["status"] == "terminated"


def test_tampered_proposal_is_refused(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    tampered = build_proposal("update_coverage", {"policy_id": "POL-001", "changes": {"annual_limit": "5000.00"}},
                              details={"operation": "coverage_change"})
    tampered["args"]["changes"]["annual_limit"] = "10000.00"
    proposals._proposals["TF"] = tampered
    workflow.phase = "insurer_decision"
    workflow.facts["request_category"] = "coverage_change"

    final = asyncio.run(handle({"decision": "approve"}, workflow, domain, proposals, log, None))

    assert (final["agent_state"], final["request_outcome"]) == ("failed", "technical_failure")
    assert policy_of(store, run_id)["financial_terms"]["annual_limit"] == "3000.00"
    assert "update_coverage" not in [name for name, _ in domain.calls]


def test_unfixable_tool_error_stops_after_three_attempts_with_a_handover(harness):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log, forced_error={
        "code": "INVALID_ARGUMENT", "message": "'3339998888' does not match the pattern", "retryable": True,
        "effect_applied": False, "field": "changes.phone"})
    extractor = StubExtractor({"request_category": "contact_update", "contact_fields": ["phone"],
                               "phone": "+393339998888", "summary": "phone"})

    final = asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))

    assert (final["agent_state"], final["request_outcome"]) == ("closed_without_execution", "handover_required")
    assert len([name for name, _ in domain.calls if name == "update_contact_details"]) <= 4
    assert any(event["kind"] == "handover" for event in log.record["events"])


def test_money_representation_error_is_corrected_and_retried(harness):
    """The executor repairs only the representation, then the real tool call succeeds."""
    store, run_id, log, workflow, proposals = harness

    class StrictOnce(LocalDomain):
        rejected = False

        async def call(self, name, arguments):
            if name == "update_coverage" and not self.rejected:
                self.rejected = True
                self.calls.append((name, copy.deepcopy(arguments)))
                return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": "not an allowed value",
                                               "retryable": True, "effect_applied": False,
                                               "field": "changes.annual_limit",
                                               "allowed_values": ["3000.00", "5000.00", "10000.00"]}}
            return await super().call(name, arguments)

    domain = StrictOnce(store, run_id, log)
    decision = Decision("coverage_change", "completed", "updated", "test", "test",
                        effect={"tool": "update_coverage",
                                "args": {"policy_id": "POL-001", "changes": {"annual_limit": "5000"}}})

    final = asyncio.run(_execute(decision, workflow, domain, log))

    assert (final["agent_state"], final["request_outcome"]) == ("completed", "updated")
    assert policy_of(store, run_id)["financial_terms"]["annual_limit"] == "5000.00"
    assert [arguments["changes"]["annual_limit"] for name, arguments in domain.calls] == ["5000", "5000.00"]


def test_correct_arguments_repairs_only_the_representation():
    arguments = {"policy_id": "POL-001", "changes": {"annual_limit": "5000"}}
    failure = {"code": "INVALID_ARGUMENT", "field": "changes.annual_limit",
               "allowed_values": ["3000.00", "5000.00", "10000.00"]}
    assert correct_arguments(arguments, failure)["changes"]["annual_limit"] == "5000.00"
    assert correct_arguments({"changes": {"phone": "+39333"}},
                             {"field": "changes.phone"}) is None
    assert correct_arguments({"changes": {"annual_limit": "5000.00"}}, {"field": "unknown.path"}) is None


def test_log_stays_valid_for_every_flow(harness, tmp_path):
    store, run_id, log, workflow, proposals = harness
    domain = LocalDomain(store, run_id, log)
    extractor = StubExtractor({"request_category": "policy_enquiry", "summary": "enquiry"})

    final = asyncio.run(handle(REQUEST, workflow, domain, proposals, log, extractor))
    log.snapshot(store.snapshot(run_id))

    assert final["request_outcome"] == "answered"
    saved = json.loads(log.path.read_text(encoding="utf-8"))
    assert saved["agent_state"] == "completed" and saved["domain_snapshot"]["policies"]["POL-001"]["status"] == "active"
