"""Schemas on persisted state and port values: invalid data fails where it is written or loaded."""

import json

import pytest
from pydantic import ValidationError

from app.workflow.facts import FACTS_BY_CATEGORY, CoverageChangeFacts
from app.workflow.ports import Action, AuthorizationPlan, Call, Decision, Findings, RouteResult
from app.workflow.record import Clarification, Workflow
from app.workflow.schema import Vocabulary
from app.workflow.states import MAX_REROUTES, Agent, Outcome
from app.workflow.store import WorkflowStore

REQUEST = {"event_type": "request", "event_id": "EV-1", "ticket_id": "T1", "policy_id": "POL-001",
           "author_name": "Taylor Example", "timestamp": "2026-10-22T10:00:00+02:00", "text": "Change my phone"}
COVERAGE = Call(tool="update_coverage", args={"policy_id": "POL-001", "changes": {"annual_limit": "10000.00"}})


class TestWorkflowFile:
    @staticmethod
    def saved(tmp_path, **overrides):
        """Write a workflow file with some fields replaced, as if edited or written by an older version."""
        store = WorkflowStore(tmp_path)
        store.save(Workflow.start(REQUEST, "RUN-test"))
        path = store.path("T1")
        path.write_text(json.dumps({**json.loads(path.read_text()), **overrides}))
        return store

    def test_json_schema_describes_the_nested_state(self):
        definitions = Workflow.model_json_schema()["$defs"]
        assert {"Clarification", "LedgerEntry", "Transition", "Proposal", "Decision", "Call"} <= set(definitions)

    @pytest.mark.parametrize("overrides", [
        {"unexpected": 1},
        {"state": "SLEEPING"},
        {"outcome": "maybe"},
        {"schema_version": 2},
        {"handling_agent": "finance"},
        {"request_category": "iban_change"},
        {"reroutes": MAX_REROUTES + 1},
        {"reroutes": -1},
        {"suspended_at": "2026-10-22T10:00:00"},  # No timezone: the 14/7-day timeouts would be ambiguous.
        {"correction_attempts": {"update_coverage:changes": -1}},
        {"history": [{"from": "RECEIVED", "to": "ROUTED", "outcome": "in_progress", "reason": "x"}]},
        {"write_ledger": [{"tool": "pay_claim", "args": {}, "hash": "abc", "status": "done"}]},
        {"clarification": {"question": "", "missing": ["phone"]}},
    ])
    def test_invalid_file_is_refused_on_load(self, tmp_path, overrides):
        with pytest.raises(ValidationError):
            self.saved(tmp_path, **overrides).load("T1")

    def test_valid_file_round_trips(self, tmp_path):
        store = self.saved(tmp_path, suspended_at="2026-10-22T10:00:00+02:00", request_category="contact_update")
        loaded = store.load("T1")
        store.save(loaded)
        assert store.load("T1") == loaded

    @pytest.mark.parametrize("field, value", [("state", "SLEEPING"), ("request_category", "iban_change"),
                                              ("reroutes", MAX_REROUTES + 1), ("ticket_id", "../escape"),
                                              ("suspended_at", "2026-10-22T10:00:00")])
    def test_invalid_assignment_fails_where_it_is_written(self, field, value):
        workflow = Workflow.start(REQUEST, "RUN-test")
        with pytest.raises(ValidationError):
            setattr(workflow, field, value)

    def test_clarification_needs_one_question_and_something_missing(self):
        with pytest.raises(ValidationError):
            Clarification(question="Which phone?", missing=[])


class TestPortValues:
    def test_calls_are_immutable_and_closed(self):
        with pytest.raises(ValidationError):
            COVERAGE.tool = "terminate_policy"
        with pytest.raises(ValidationError):
            Call(tool="update_coverage", args={}, run_id="RUN-1")

    @pytest.mark.parametrize("decision", [
        {"action": Action.ASK_AUTHORIZATION},
        {"action": Action.EXECUTE, "authorization": AuthorizationPlan(summary="x", outcome=Outcome.UPDATED,
                                                                       on_approve=(COVERAGE,))},
    ])
    def test_authorization_plan_only_with_ask_authorization(self, decision):
        with pytest.raises(ValidationError, match="ask_authorization"):
            Decision(**decision)

    @pytest.mark.parametrize("build", [
        lambda: RouteResult(agent=Agent.CLAIMS, confidence=1.5),
        lambda: RouteResult(agent="finance"),
        lambda: RouteResult(agent=Agent.CLAIMS, category_hint="iban_change"),
        lambda: Findings(category="not_in_vocabulary"),
        lambda: AuthorizationPlan(summary="", outcome=Outcome.UPDATED, on_approve=(COVERAGE,)),
    ])
    def test_values_outside_the_contract_are_refused(self, build):
        with pytest.raises(ValidationError):
            build()


class TestFactsPerCategory:
    def test_every_vocabulary_category_has_a_facts_model(self):
        assert set(FACTS_BY_CATEGORY) == Vocabulary.CATEGORIES

    @pytest.mark.parametrize("category, facts", [
        ("contact_update", {"amount": "180.00"}),                 # Key from another operation.
        ("reimbursement_request", {"phone": "+391234567"}),
        ("reimbursement_request", {"expense": 1600}),             # Values stay strings, as given.
        ("policy_termination", {"explicit_instruction": "maybe"}),
    ])
    def test_findings_with_facts_the_category_does_not_know_are_refused(self, category, facts):
        with pytest.raises(ValidationError):
            Findings(category=category, facts=facts)

    def test_closing_and_reroute_categories_ignore_stray_facts(self):
        assert Findings(category="out_of_scope_claims", facts={"expense": "999.00"}).category

    def test_reclassification_drops_facts_the_new_category_does_not_know(self):
        workflow = Workflow.start(REQUEST, "RUN-test")
        workflow.classify("contact_update")
        workflow.merge_facts({"phone": "+391234567", "email": "a@b.it"})
        workflow.classify("policy_enquiry")
        assert (workflow.request_category, workflow.facts) == ("policy_enquiry", {})

    def test_foreign_facts_fail_where_they_are_written(self):
        workflow = Workflow.start(REQUEST, "RUN-test")
        workflow.classify("coverage_change")
        with pytest.raises(ValidationError):
            workflow.merge_facts({"phone": "+391234567"})

    def test_file_with_facts_outside_its_category_is_refused_on_load(self, tmp_path):
        store = TestWorkflowFile.saved(tmp_path, request_category="coverage_change", facts={"phone": "+3912"})
        with pytest.raises(ValidationError):
            store.load("T1")

    def test_typed_facts_give_attributes_to_the_rules(self):
        workflow = Workflow.start(REQUEST, "RUN-test")
        workflow.classify("coverage_change")
        workflow.merge_facts({"deductible": "100.00"})
        assert workflow.typed_facts == CoverageChangeFacts(deductible="100.00")
        assert workflow.typed_facts.annual_limit is None
