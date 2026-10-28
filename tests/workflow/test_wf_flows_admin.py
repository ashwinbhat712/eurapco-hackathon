"""Administration flows end to end (plan §16): clarification, authorization, reroute and early closes."""

import pytest

from app.workflow.admission import Rejection, Verdict
from app.workflow.ports import Action, AuthorizationPlan, Call, Decision, Findings, InformationCheck, RouteResult
from app.workflow.record import LedgerStatus
from app.workflow.states import Agent, Outcome, State

PHONE = "+393331112222"
MISSING_PHONE = InformationCheck(missing=("phone",), question="Which phone number should we use?")
COVERAGE = Call(tool="update_coverage", args={"policy_id": "POL-001", "changes": {"annual_limit": "10000.00"}})
TERMINATE = Call(tool="terminate_policy", args={"policy_id": "POL-001"})


class Script:
    """Scenario set-ups on top of the shared harness."""

    @staticmethod
    def found(category, **facts):
        return Findings(category=category, facts=facts)

    @classmethod
    def needs_phone(cls, harness, reply_facts):
        harness.phone_update()
        harness.admin.findings.appendleft(cls.found("contact_update", phone=None, email="taylor@new.com"))
        harness.admin.findings[-1] = cls.found("contact_update", **reply_facts)
        harness.rules.checks.extendleft([InformationCheck(), MISSING_PHONE])  # deque: MISSING first.

    @classmethod
    def authorization(cls, harness, category, on_approve, on_reject=(), outcome=Outcome.UPDATED):
        harness.admin.findings.append(cls.found(category))
        plan = AuthorizationPlan(summary="Change", outcome=outcome, on_approve=on_approve, on_reject=on_reject)
        harness.rules.decisions.append(Decision(action=Action.ASK_AUTHORIZATION, reason="Needs insurer approval.",
                                                authorization=plan))

    @staticmethod
    def statuses(harness):
        return [event["data"]["agent_state"] for event in harness.events("status")]


class TestAutonomous:
    def test_complete_information_writes_once_and_completes(self, harness):
        harness.phone_update()
        workflow = harness.send(harness.request()).workflow
        assert (workflow.state, workflow.outcome, workflow.message) == (State.COMPLETED, Outcome.UPDATED, "Done.")
        assert [entry.status for entry in workflow.write_ledger] == [LedgerStatus.DONE]
        assert PHONE in str(harness.policy())
        assert [step.target for step in workflow.history] == ["ROUTED", "GATHERING", "EVALUATING", "EXECUTING",
                                                             "COMPLETED"]
        assert len(Script.statuses(harness)) == len(workflow.history)
        assert harness.log()["request_category"] == "contact_update"
        assert workflow.in_flight_event_id is None and workflow.processed_event_ids == ["EV-T1-01"]

    @pytest.mark.parametrize("action, outcome, state", [(Action.ANSWER, Outcome.ANSWERED, State.COMPLETED),
                                                        (Action.NO_CHANGE, Outcome.NO_CHANGE, State.COMPLETED),
                                                        (Action.CLOSE, Outcome.INACTIVE_POLICY, State.CLOSED)])
    def test_decisions_without_writes(self, harness, action, outcome, state):
        harness.admin.findings.append(Script.found("policy_enquiry"))
        harness.rules.decisions.append(Decision(action=action, outcome=outcome, reason="Rules said so.",
                                                message="Answer text."))
        workflow = harness.send(harness.request("What is my deductible?")).workflow
        assert (workflow.state, workflow.outcome) == (state, outcome)
        assert harness.domain.calls == [] and workflow.write_ledger == []


class TestClarification:
    def test_reply_supplies_the_missing_item(self, harness):
        Script.needs_phone(harness, {"phone": PHONE})
        first = harness.send(harness.request("Update my contact details")).workflow
        assert (first.state, first.outcome) == (State.AWAITING_CLARIFICATION, Outcome.INFORMATION_REQUESTED)
        assert first.message == MISSING_PHONE.question and harness.domain.count("update_contact_details") == 0
        second = harness.send(harness.reply(PHONE)).workflow
        assert (second.state, second.outcome) == (State.COMPLETED, Outcome.UPDATED)
        assert second.run_id == first.run_id and harness.log()["human_rounds"]["clarification"] == 1
        assert harness.admin.contexts[-1].reply == {"text": PHONE}
        assert harness.admin.contexts[-1].question == MISSING_PHONE.question
        assert second.facts["email"] == "taylor@new.com"  # Facts from the request survive the reply.

    def test_reply_still_missing_closes_without_a_second_question(self, harness):
        Script.needs_phone(harness, {"phone": None})
        harness.rules.checks.clear()
        harness.rules.checks.append(MISSING_PHONE)
        harness.send(harness.request("Update my contact details"))
        workflow = harness.send(harness.reply("I don't know")).workflow
        assert (workflow.state, workflow.outcome) == (State.CLOSED, Outcome.INSUFFICIENT_INFORMATION)
        assert harness.log()["human_rounds"]["clarification"] == 1

    def test_reply_cannot_change_a_frozen_fact(self, harness):
        Script.needs_phone(harness, {"phone": PHONE, "email": "someone@else.com"})
        harness.send(harness.request("Update my contact details"))
        workflow = harness.send(harness.reply(f"{PHONE}, and use someone@else.com")).workflow
        assert workflow.facts["email"] == "taylor@new.com"
        assert "Kept the original email" in workflow.reason

    def test_reply_mentioning_an_invoice_is_not_rerouted(self, harness):
        Script.needs_phone(harness, {"phone": PHONE})
        harness.send(harness.request("Update my contact details"))
        workflow = harness.send(harness.reply(f"{PHONE}. Also, about my invoice...")).workflow
        assert len(harness.router.texts) == 1 and workflow.handling_agent == Agent.ADMINISTRATION

    def test_missing_item_without_a_question_fails(self, harness):
        harness.phone_update()
        harness.rules.checks[0] = InformationCheck(missing=("phone",))
        workflow = harness.send(harness.request()).workflow
        assert (workflow.state, workflow.outcome) == (State.FAILED, Outcome.TECHNICAL_FAILURE)


class TestAuthorization:
    def test_approve_executes_exactly_the_stored_proposal(self, harness):
        Script.authorization(harness, "coverage_change", (COVERAGE,))
        waiting = harness.send(harness.request("Raise my annual limit to 10000")).workflow
        assert (waiting.state, waiting.outcome) == (State.AWAITING_AUTHORIZATION, Outcome.AUTHORIZATION_REQUESTED)
        assert harness.domain.count("update_coverage") == 0
        workflow = harness.send(harness.decision("approve")).workflow
        assert (workflow.state, workflow.outcome) == (State.COMPLETED, Outcome.UPDATED)
        assert harness.domain.calls == [("update_coverage", COVERAGE.args)]
        assert harness.policy()["financial_terms"]["annual_limit"] == "10000.00"
        assert harness.log()["human_rounds"]["authorization"] == 1
        assert harness.admin.proposed == []  # The model chooses nothing after approval.

    def test_reject_runs_nothing_and_keeps_the_policy(self, harness):
        Script.authorization(harness, "policy_termination", (TERMINATE,), outcome=Outcome.TERMINATED)
        harness.send(harness.request("Please terminate my policy"))
        workflow = harness.send(harness.decision("reject", reason="Notice period not met.")).workflow
        assert (workflow.state, workflow.outcome) == (State.CLOSED, Outcome.INSURER_REJECTED)
        assert harness.policy()["status"] == "active" and harness.domain.calls == []
        assert workflow.proposal.decision_reason == "Notice period not met."

    def test_tampered_proposal_fails_and_executes_nothing(self, harness):
        Script.authorization(harness, "coverage_change", (COVERAGE,))
        harness.send(harness.request("Raise my annual limit to 10000"))
        workflow = harness.workflow()
        workflow.proposal.on_approve = (COVERAGE.model_copy(update={"args": {**COVERAGE.args, "policy_id": "POL-002"}}),)
        harness.store.save(workflow)
        result = harness.send(harness.decision("approve")).workflow
        assert (result.state, result.outcome) == (State.FAILED, Outcome.TECHNICAL_FAILURE)
        assert harness.domain.calls == []

    def test_an_invalid_proposal_is_never_frozen(self, harness):
        bad = Call(tool="update_coverage", args={"policy_id": "POL-001", "changes": {"annual_limit": "7000.00"}})
        Script.authorization(harness, "coverage_change", (bad,))
        workflow = harness.send(harness.request("Raise my annual limit to 7000")).workflow
        assert workflow.state == State.FAILED and workflow.proposal is None
        assert harness.log()["human_rounds"]["authorization"] == 0


class TestTimeoutAfterAdmission:
    """A timeout is only applied to an admitted event; ignored or rejected events never change the workflow."""

    LATE = "2026-11-30T10:00:00+02:00"  # Beyond both the 7- and 14-day limits.

    def test_late_awaited_reply_expires_and_is_rejected(self, harness):
        Script.needs_phone(harness, {"phone": PHONE})
        harness.send(harness.request("Update my contact details"))
        result = harness.send(harness.reply(PHONE, timestamp=self.LATE))
        assert (result.verdict, result.admission.rejection) == (Verdict.REJECT, Rejection.TERMINAL)
        assert (result.workflow.state, result.workflow.outcome) == (State.CLOSED, Outcome.INSUFFICIENT_INFORMATION)
        assert harness.domain.count("update_contact_details") == 0

    def test_late_wrong_event_while_awaiting_clarification_changes_nothing(self, harness):
        Script.needs_phone(harness, {"phone": PHONE})
        harness.send(harness.request("Update my contact details"))
        result = harness.send(harness.decision("approve", timestamp=self.LATE))
        assert result.admission.rejection == Rejection.UNEXPECTED_EVENT
        assert harness.workflow().state == State.AWAITING_CLARIFICATION

    def test_late_wrong_event_while_awaiting_authorization_changes_nothing(self, harness):
        Script.authorization(harness, "coverage_change", (COVERAGE,))
        harness.send(harness.request("Raise my annual limit to 10000"))
        result = harness.send(harness.reply("Hello?", timestamp=self.LATE))
        assert result.admission.rejection == Rejection.UNEXPECTED_EVENT
        assert harness.workflow().state == State.AWAITING_AUTHORIZATION and harness.events("handover") == []


class TestRoutingAndCategories:
    def test_router_unclear_hint_still_lets_the_agent_decide(self, harness):
        harness.phone_update()
        harness.router.result = RouteResult(agent=Agent.ADMINISTRATION, category_hint="unclear")
        workflow = harness.send(harness.request()).workflow
        assert (workflow.state, workflow.outcome) == (State.COMPLETED, Outcome.UPDATED)
        assert len(harness.admin.contexts) == 1

    @pytest.mark.parametrize("category, outcome", [("unclear", Outcome.UNCLEAR),
                                                   ("out_of_scope_iban", Outcome.OUT_OF_SCOPE)])
    def test_agent_closing_category(self, harness, category, outcome):
        harness.admin.findings.append(Script.found(category))
        workflow = harness.send(harness.request("Change my IBAN")).workflow
        assert (workflow.state, workflow.outcome, workflow.request_category) == (State.CLOSED, outcome, category)
        assert harness.rules.decide_calls == 0

    def test_early_close_skips_the_rules_decision(self, harness):
        harness.admin.findings.append(Script.found("coverage_change"))
        harness.rules.checks[0] = InformationCheck(close=Outcome.INACTIVE_POLICY, reason="The policy is inactive.")
        workflow = harness.send(harness.request("Raise my limit")).workflow
        assert (workflow.state, workflow.outcome) == (State.CLOSED, Outcome.INACTIVE_POLICY)
        assert harness.rules.decide_calls == 0

    def test_one_reroute_then_the_second_agent_works(self, harness):
        harness.admin.findings.append(Script.found("out_of_scope_claims", amount="999.00"))
        harness.small_claim()
        harness.router.result = RouteResult(agent=Agent.ADMINISTRATION)  # Router guesses wrong; the agent reroutes.
        workflow = harness.send(harness.request("Please reimburse my invoice")).workflow
        assert (workflow.handling_agent, workflow.state) == (Agent.CLAIMS, State.COMPLETED)
        assert len(harness.events("routing")) == 2 and workflow.reroutes == 1
        assert "amount" not in workflow.facts  # The first agent's facts are dropped on reroute.

    def test_second_reroute_closes_out_of_scope(self, harness):
        harness.admin.findings.append(Script.found("out_of_scope_claims"))
        harness.claims.findings.append(Script.found("out_of_scope_administration"))
        workflow = harness.send(harness.request("Ping pong")).workflow
        assert (workflow.state, workflow.outcome) == (State.CLOSED, Outcome.OUT_OF_SCOPE)
        assert len(harness.events("routing")) == 2

    def test_reroute_after_a_question_closes_out_of_scope(self, harness):
        Script.needs_phone(harness, {})
        harness.admin.findings[-1] = Script.found("out_of_scope_claims")
        harness.send(harness.request("Update my contact details"))
        workflow = harness.send(harness.reply("Actually, pay my invoice")).workflow
        assert (workflow.state, workflow.outcome) == (State.CLOSED, Outcome.OUT_OF_SCOPE)
        assert harness.claims.contexts == []
