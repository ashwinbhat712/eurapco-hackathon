"""Event admission (plan §9), proposal freezing (§11) and the persisted record (§6)."""

import pytest

from app.workflow.admission import AdmissionPolicy, Rejection, Verdict
from app.workflow.ports import Action, AuthorizationPlan, Call, Decision
from app.workflow.proposal import CallCheck, Proposal, ProposalError, ProposalStatus
from app.workflow.record import Workflow
from app.workflow.states import Outcome, State
from app.workflow.store import WorkflowStore

REQUEST = {"event_type": "request", "event_id": "EV-1", "ticket_id": "T1", "policy_id": "POL-001",
           "author_name": "Taylor Example", "timestamp": "2026-10-22T10:00:00+02:00", "text": "Change my phone"}
REPLY = {"event_type": "clarification_reply", "event_id": "EV-2", "ticket_id": "T1",
         "timestamp": "2026-10-22T10:01:00+02:00", "text": "10100"}
DECISION = {"event_type": "insurer_decision", "event_id": "EV-3", "ticket_id": "T1",
            "timestamp": "2026-10-22T10:02:00+02:00", "decision": "approve"}
COVERAGE = Call(tool="update_coverage", args={"policy_id": "POL-001", "changes": {"annual_limit": "10000.00"}})
REJECT = Call(tool="record_claim_outcome",
              args={"policy_id": "POL-001", "claim_id": "CL-1", "outcome": "rejected", "reason": "insurer"})


class Plans:
    @staticmethod
    def of(*on_approve, on_reject=(), outcome=Outcome.UPDATED, summary="x"):
        return AuthorizationPlan(summary=summary, outcome=outcome, on_approve=on_approve, on_reject=on_reject)


class TestAdmission:
    @staticmethod
    def workflow(state, in_flight=None):
        workflow = Workflow.start(REQUEST, "RUN-test")
        workflow.finish_event()
        workflow.state, workflow.in_flight_event_id = state, in_flight
        return workflow

    @pytest.mark.parametrize("event, state, verdict, rejection", [
        (REPLY, State.AWAITING_CLARIFICATION, Verdict.ADMIT, None),
        (DECISION, State.AWAITING_AUTHORIZATION, Verdict.ADMIT, None),
        (REQUEST, State.AWAITING_CLARIFICATION, Verdict.IGNORE, Rejection.DUPLICATE_EVENT),
        ({**REQUEST, "event_id": "EV-9"}, State.AWAITING_CLARIFICATION, Verdict.REJECT, Rejection.TICKET_ACTIVE),
        (DECISION, State.AWAITING_CLARIFICATION, Verdict.REJECT, Rejection.UNEXPECTED_EVENT),
        (REPLY, State.AWAITING_AUTHORIZATION, Verdict.REJECT, Rejection.UNEXPECTED_EVENT),
        (REPLY, State.GATHERING, Verdict.REJECT, Rejection.IN_FLIGHT),
        (DECISION, State.EXECUTING, Verdict.REJECT, Rejection.IN_FLIGHT),
        (REPLY, State.COMPLETED, Verdict.REJECT, Rejection.TERMINAL),
        (DECISION, State.CLOSED, Verdict.REJECT, Rejection.TERMINAL),
        (REPLY, State.HANDOVER, Verdict.REJECT, Rejection.TERMINAL),
        (DECISION, State.FAILED, Verdict.REJECT, Rejection.TERMINAL),
        ({**REQUEST, "event_id": "EV-9"}, State.COMPLETED, Verdict.REJECT, Rejection.TERMINAL),
    ])
    def test_table(self, event, state, verdict, rejection):
        admission = AdmissionPolicy().admit(event, self.workflow(state))
        assert (admission.verdict, admission.rejection) == (verdict, rejection)

    def test_new_request_without_workflow_is_admitted(self):
        assert AdmissionPolicy().admit(REQUEST, None).verdict == Verdict.ADMIT

    @pytest.mark.parametrize("event", [REPLY, DECISION])
    def test_reply_or_decision_for_unknown_ticket_is_rejected(self, event):
        assert AdmissionPolicy().admit(event, None).rejection == Rejection.UNKNOWN_TICKET

    def test_reply_while_another_event_is_in_flight_is_rejected(self):
        workflow = self.workflow(State.AWAITING_CLARIFICATION, in_flight="EV-OTHER")
        assert AdmissionPolicy().admit(REPLY, workflow).rejection == Rejection.IN_FLIGHT

    def test_the_interrupted_event_itself_recovers(self):
        workflow = self.workflow(State.EXECUTING, in_flight=REPLY["event_id"])
        assert AdmissionPolicy().admit(REPLY, workflow).verdict == Verdict.RECOVER

    def test_processed_event_is_ignored_even_after_terminal(self):
        assert AdmissionPolicy().admit(REQUEST, self.workflow(State.COMPLETED)).verdict == Verdict.IGNORE

    @pytest.mark.parametrize("state, verdict", [(State.COMPLETED, Verdict.ADMIT),
                                                (State.AWAITING_CLARIFICATION, Verdict.IGNORE)])
    def test_fresh_rerun_only_restarts_terminal_tickets(self, state, verdict):
        assert AdmissionPolicy(fresh=True).admit(REQUEST, self.workflow(state)).verdict == verdict


class TestProposal:
    @staticmethod
    def workflow():
        return Workflow.start(REQUEST, "RUN-test")

    def test_freeze_hashes_both_branches_and_verifies(self):
        proposal = Proposal.freeze(self.workflow(), Plans.of(COVERAGE, summary="Raise limit"), [])
        assert proposal.status == ProposalStatus.PENDING and proposal.verify()
        assert proposal.hash == Proposal.content_hash("Raise limit", Outcome.UPDATED, (COVERAGE,), ())

    def test_moving_a_call_between_branches_fails_verification(self):
        proposal = Proposal.freeze(self.workflow(), Plans.of(COVERAGE, REJECT, outcome=Outcome.PAID), [])
        proposal.on_approve, proposal.on_reject = (COVERAGE,), (REJECT,)
        assert proposal.verify() is False

    @pytest.mark.parametrize("field, value", [("summary", "Lower limit"), ("outcome", Outcome.TERMINATED)])
    def test_tampered_summary_or_outcome_fails_verification(self, field, value):
        proposal = Proposal.freeze(self.workflow(), Plans.of(COVERAGE, summary="Raise limit"), [])
        setattr(proposal, field, value)
        assert proposal.verify() is False

    def test_hash_is_independent_of_key_order(self):
        reordered = Call(tool="update_coverage",
                         args={"changes": {"annual_limit": "10000.00"}, "policy_id": "POL-001"})
        assert CallCheck.digest((COVERAGE,)) == CallCheck.digest((reordered,))

    @pytest.mark.parametrize("branch", ["on_approve", "on_reject"])
    def test_tampered_branch_fails_verification(self, branch):
        proposal = Proposal.freeze(self.workflow(), Plans.of(COVERAGE, on_reject=(REJECT,), outcome=Outcome.PAID), [])
        original = getattr(proposal, branch)[0]
        tampered = original.model_copy(update={"args": {**original.args, "policy_id": "POL-002"}})
        setattr(proposal, branch, (tampered,))
        assert proposal.verify() is False

    def test_references_bind_to_prep_results(self):
        pay = Call(tool="pay_claim",
                   args={"policy_id": "POL-001", "claim_id": Call.ref(0, "claim_id"), "amount": "1560.00"})
        proposal = Proposal.freeze(self.workflow(), Plans.of(pay, outcome=Outcome.PAID, summary="Pay"),
                                   [{"ok": True, "data": {"claim_id": "CL-abc"}}])
        assert proposal.on_approve[0].args["claim_id"] == "CL-abc"

    @pytest.mark.parametrize("call, message", [
        (Call(tool="update_coverage", args={"policy_id": "POL-001", "changes": {"annual_limit": "10000"}}),
         "invalid"),
        (Call(tool="update_coverage", args={"policy_id": "POL-002", "changes": {"annual_limit": "10000.00"}}),
         "own policy"),
        (Call(tool="get_policy", args={"policy_id": "POL-001"}), "not a write tool"),
        (Call(tool="pay_claim", args={"policy_id": "POL-001", "claim_id": "CL-1", "amount": "1560"}), "invalid"),
    ])
    def test_invalid_calls_are_never_frozen(self, call, message):
        workflow = self.workflow()
        with pytest.raises(ProposalError, match=message):
            Proposal.freeze(workflow, Plans.of(call), [])
        assert workflow.proposal is None

    def test_only_one_proposal_per_ticket(self):
        workflow = self.workflow()
        workflow.proposal = Proposal.freeze(workflow, Plans.of(COVERAGE), [])
        with pytest.raises(ProposalError, match="Only one"):
            Proposal.freeze(workflow, Plans.of(COVERAGE), [])

    def test_proposal_without_calls_is_refused(self):
        with pytest.raises(ProposalError, match="at least one"):
            Proposal.freeze(self.workflow(), Plans.of(), [])

    def test_rejection_calls_carry_the_insurer_reason(self):
        proposal = Proposal.freeze(self.workflow(), Plans.of(COVERAGE, on_reject=(REJECT,), outcome=Outcome.PAID), [])
        assert proposal.rejection_calls("Over budget")[0].args["reason"] == "Over budget"
        assert proposal.verify()  # The stored branch is unchanged.


class TestRecordAndStore:
    def test_round_trip_keeps_proposal_decision_and_state(self, tmp_path):
        store = WorkflowStore(tmp_path)
        workflow = Workflow.start(REQUEST, "RUN-test")
        workflow.state = State.AWAITING_AUTHORIZATION
        workflow.decision = Decision(action=Action.ASK_AUTHORIZATION, reason="Above threshold",
                                     authorization=Plans.of(COVERAGE))
        workflow.proposal = Proposal.freeze(workflow, workflow.decision.authorization, [])
        store.save(workflow)
        loaded = store.load("T1")
        assert loaded == workflow
        assert loaded.proposal.verify() and loaded.decision.authorization.on_approve == (COVERAGE,)

    @pytest.mark.parametrize("ticket", ["../escape", "a/b", "", "x" * 101, "T1.json"])
    def test_ticket_id_cannot_escape_the_directory(self, tmp_path, ticket):
        with pytest.raises(ValueError, match="ticket_id"):
            WorkflowStore(tmp_path).path(ticket)

    def test_missing_workflow_loads_as_none(self, tmp_path):
        assert WorkflowStore(tmp_path).load("T404") is None

    @pytest.mark.parametrize("original, new, expected, noted", [
        ({"amount": "180.00"}, {"amount": "200.00"}, {"amount": "180.00"}, True),
        ({"amount": "180.00"}, {"amount": "180.00", "postal_code": "10100"},
         {"amount": "180.00", "postal_code": "10100"}, False),
        ({"postal_code": None}, {"postal_code": "10100"}, {"postal_code": "10100"}, False),
        ({"postal_code": ""}, {"postal_code": "10100"}, {"postal_code": "10100"}, False),
        ({"street": "8 Orchard Road"}, {"street": None}, {"street": "8 Orchard Road"}, False),
    ])
    def test_frozen_facts_are_only_filled_never_changed(self, original, new, expected, noted):
        workflow = Workflow.start(REQUEST, "RUN-test")
        workflow.facts = original
        workflow.merge_facts(new)
        assert workflow.facts == expected
        assert bool(workflow.notes) is noted
