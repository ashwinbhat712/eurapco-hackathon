"""Claims workflow over the real domain store (no model): effects, records, payments and responses."""

import asyncio
import json

import pytest

import app.runtime as runtime
from app.claims_engine import facts_from_extraction
from app.claims_extraction import ClaimExtraction, extract_claim, system_prompt
from app.claims_workflow import ReimbursementWorkflow
from app.contracts import ROOT, validator
from app.domain import DomainStore
from app.evaluation import EvaluationLog
from app.skills import load_skill, load_skills
from app.subagents import SubagentRun
from app.supervisor import RoutingDecision


def ticket(name):
    return json.loads((ROOT / f"examples/training/{name}.json").read_text(encoding="utf-8"))


def invoice_extraction(provider, number, invoice_date, service_date, patient, service, category, amount,
                       prescription=None):
    data = {"service_category": category, "provider_id": provider, "invoice_number": number,
            "invoice_date": invoice_date, "service_date": service_date, "patient_name": patient,
            "service_description": service, "amount": amount, "currency": "EUR", "paid": True}
    if prescription:
        data.update(prescription_patient_name=patient, prescription_service_category=category)
    return ClaimExtraction(**data)


# What a correct extractor returns for each public claims ticket.
EXTRACTIONS = {
    "T08": invoice_extraction("PROVIDER-002", "INV-2026-008", "2026-10-20", "2026-10-20", "Taylor Example",
                              "specialist consultation", "specialist_consultation", "180.00"),
    "T09": invoice_extraction("PROVIDER-002", "INV-2026-009", "2026-10-20", "2026-10-20", "Taylor Example",
                              "diagnostic blood test", "diagnostic_test", "300.00"),
    "T10": invoice_extraction("PROVIDER-002", "INV-2026-010", "2026-12-31", "2026-12-31", "Taylor Example",
                              "physiotherapy", "physiotherapy", "1925.00", prescription=True),
    "T11": invoice_extraction("PROVIDER-002", "INV-2026-011", "2026-10-20", "2026-10-20", "Taylor Example",
                              "specialist consultation", "specialist_consultation", "2000.00"),
    "T12": invoice_extraction("PROVIDER-002", "INV-2026-012", "2026-10-20", "2026-10-20", "Morgan Sample",
                              "diagnostic MRI", "diagnostic_test", "2000.00", prescription=True),
    "T13": invoice_extraction("PROVIDER-001", "INV-2026-001", "2026-10-02", "2026-10-01", "Taylor Example",
                              "specialist consultation", "specialist_consultation", "180.00"),
    "T14": ClaimExtraction(service_category="dental_care"),
    "T15": invoice_extraction("PROVIDER-002", "INV-2026-015", "2026-10-20", "2026-10-20", "Casey Demo",
                              "specialist consultation", "specialist_consultation", "180.00"),
}


class StoreDomain:
    """DomainClient stand-in over the real store; optionally fails pay_claim like a broken processor."""

    def __init__(self, store, run_id, fail_payment=False):
        self.store, self.run_id, self.fail_payment, self.calls = store, run_id, fail_payment, []

    async def call(self, name, arguments):
        self.calls.append(name)
        if name == "pay_claim" and self.fail_payment:
            return {"ok": False, "error": {"code": "OPERATION_FAILED", "message": "processor down",
                                           "retryable": False, "effect_applied": False}}
        return self.store.call(name, {**arguments, "run_id": self.run_id})


@pytest.fixture
def store(tmp_path):
    return DomainStore(tmp_path)


def process(store, name, fail_payment=False, extraction=None):
    run_id = store.start_run()
    domain = StoreDomain(store, run_id, fail_payment)

    async def extractor(_request):
        return extraction or EXTRACTIONS[name]

    final = asyncio.run(ReimbursementWorkflow(domain, extractor).run(ticket(name)))
    validator("agent-response.schema.json").validate(final)
    state = store.snapshot(run_id)
    new_claims = [c for cid, c in state["claims"].items() if not cid.startswith("CL-H")]
    return final, state, new_claims, domain.calls


def outcome(final):
    return final["request_category"], final["agent_state"], final["request_outcome"]


def test_t08_specialist_180_is_paid_104(store):
    final, state, claims, _ = process(store, "T08")
    assert outcome(final) == ("reimbursement_request", "completed", "paid")
    assert [(c["status"], c["payment"]["amount"]) for c in claims] == [("paid", "104.00")]
    assert state["policies"]["POL-001"]["financial_terms"]["remaining_limit"] == "2792.00"
    assert "EUR 104.00" in final["message"]


def test_t10_last_coverage_day_1925_pays_exactly_1500_without_approval(store):
    final, _, claims, _ = process(store, "T10")
    assert outcome(final)[1:] == ("completed", "paid")
    assert claims[0]["payment"]["amount"] == "1500.00"
    assert claims[0]["evidence_refs"] == ["tickets/T10/EV-T10-01/invoice_text", "tickets/T10/EV-T10-01/prescription_text"]


def test_t11_above_threshold_waits_for_insurer_and_does_not_pay(store):
    final, state, claims, calls = process(store, "T11")
    assert outcome(final)[1:] == ("awaiting_authorization", "authorization_requested")
    assert "pay_claim" not in calls
    assert [(c["status"], c["payment"]) for c in claims] == [("pending_authorization", None)]
    assert final["proposal"]["amount"] == "1560.00" and final["proposal"]["operation"] == "pay_claim"
    assert final["proposal"]["arguments"]["claim_id"] == claims[0]["claim_id"]
    assert state["policies"]["POL-001"]["financial_terms"]["remaining_limit"] == "2896.00"


def test_t12_capped_by_remaining_limit_pays_900(store):
    final, state, claims, _ = process(store, "T12")
    assert outcome(final)[1:] == ("completed", "paid")
    assert claims[0]["payment"]["amount"] == "900.00"
    assert state["policies"]["POL-002"]["financial_terms"]["remaining_limit"] == "0.00"


@pytest.mark.parametrize("name,expected", [
    ("T09", ("awaiting_clarification", "information_requested")),
    ("T13", ("closed_without_execution", "duplicate")),
    ("T14", ("closed_without_execution", "not_covered")),
    ("T15", ("closed_without_execution", "inactive_policy")),
])
def test_rejections_and_questions_have_no_write_effects(store, name, expected):
    final, _, claims, calls = process(store, name)
    assert outcome(final)[1:] == expected
    assert claims == [] and set(calls) == {"get_policy", "list_claims"}


def test_t09_asks_once_for_the_prescription(store):
    final, *_ = process(store, "T09")
    assert final["question"] == ("To process your reimbursement request, please send in one reply: "
                                 "the prescription for this service.")


def test_t13_reports_existing_reference(store):
    final, *_ = process(store, "T13")
    assert "CL-H001" in final["message"]


def test_zero_reimbursement_is_recorded_without_payment(store):
    extraction = EXTRACTIONS["T08"].model_copy(update={"amount": "40.00"})
    final, _, claims, calls = process(store, "T08", extraction=extraction)
    assert outcome(final)[1:] == ("closed_without_execution", "zero_reimbursement")
    assert "pay_claim" not in calls and [(c["status"], c["payment"]) for c in claims] == [("rejected", None)]


def test_failed_payment_is_never_reported_as_paid(store):
    final, state, claims, _ = process(store, "T08", fail_payment=True)
    assert outcome(final)[1:] == ("failed", "technical_failure")
    assert [(c["status"], c["payment"]) for c in claims] == [("payment_failed", None)]
    assert state["policies"]["POL-001"]["financial_terms"]["remaining_limit"] == "2896.00"


def test_hallucinated_invoice_without_invoice_text_cannot_be_paid(store):
    final, _, claims, _ = process(store, "T14", extraction=EXTRACTIONS["T08"])
    assert final["request_outcome"] == "information_requested" and claims == []


# --- Skill, extraction and integration ----------------------------------------------------------------

def test_claims_skill_is_loaded_into_extractor_and_llm_agent():
    assert "reimbursement" in dict(load_skills("claims"))
    skill = load_skill("claims", "reimbursement")
    assert "Decision order" in skill and skill in system_prompt()
    prompt = SubagentRun("claims", None, None).system_prompt()
    assert "## Skill: reimbursement" in prompt and "1925.00" in prompt


def test_claims_llm_agent_has_no_write_tools():
    names = {tool.name for tool in SubagentRun("claims", None, None).tools()}
    assert names == {"get_policy", "get_claim", "list_claims"}


class ExtractionClient:
    def __init__(self, extraction):
        self.extraction, self.calls = extraction, 0

    async def a_structured_response(self, **kwargs):
        from datapizza.core.clients import ClientResponse
        from datapizza.type import StructuredBlock

        self.calls += 1
        self.system_prompt = kwargs.get("system_prompt")
        return ClientResponse(content=[StructuredBlock(content=self.extraction)])


def test_datapizza_extractor_returns_structured_facts():
    client = ExtractionClient(EXTRACTIONS["T08"])
    result = asyncio.run(extract_claim(ticket("T08"), client))
    assert result == EXTRACTIONS["T08"] and client.calls == 1
    assert facts_from_extraction(result.model_dump(), ticket("T08")).invoice_number == "INV-2026-008"


class FakeDomainClient:
    def __init__(self, run_id, agent, log):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def final_answer(category, state="completed", outcome="answered"):
    return {"request_category": category, "agent_state": state, "request_outcome": outcome,
            "reason": "r", "message": "m", "question": None, "proposal": None}


@pytest.mark.parametrize("supervisor_category,llm_category,engine_runs", [
    ("reimbursement_request", None, True),
    ("claim_enquiry", "reimbursement_request", True),  # LLM recognizes a new reimbursement -> engine
    ("claim_enquiry", "claim_enquiry", False),
])
def test_dispatch_sends_new_reimbursements_to_the_engine(monkeypatch, tmp_path, supervisor_category,
                                                         llm_category, engine_runs):
    used = []

    class FakeSubagent:
        def __init__(self, *args):
            pass

        async def run(self, request, hint=None):
            used.append("llm")
            return final_answer(llm_category, "processing", "in_progress") if llm_category == "reimbursement_request" \
                else final_answer(llm_category)

    async def fake_reimbursement(request, client, domain, log):
        used.append("engine")
        return final_answer("reimbursement_request", "completed", "paid")

    monkeypatch.setattr(runtime, "DomainClient", FakeDomainClient)
    monkeypatch.setattr(runtime, "SubagentRun", FakeSubagent)
    monkeypatch.setattr(runtime, "reimbursement", fake_reimbursement)
    log = EvaluationLog(ticket("T08"), "RUN-test", "TEAM-1", tmp_path)
    decision = RoutingDecision(agent="claims", request_category=supervisor_category, rationale="x")
    asyncio.run(runtime.dispatch(ticket("T08"), "RUN-test", log, None, decision))
    assert ("engine" in used) is engine_runs
    assert ("llm" in used) is (supervisor_category != "reimbursement_request")
