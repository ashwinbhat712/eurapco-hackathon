"""Unit tests: event admission (plan §9), the frozen proposal (§11), the workflow record and store (§6)."""

import pytest

from app.workflow.admission import AdmissionPolicy, Rejection, Verdict
from app.workflow.ports import AuthorizationPlan, Call, Decision
from app.workflow.proposal import CallCheck, Proposal, ProposalError
from app.workflow.record import Workflow
from app.workflow.states import State
from app.workflow.store import WorkflowStore
from harness.fakes import POLICY, Calls, Decisions, Events


class Records:
    @staticmethod
    def flow(state, processed=("EV-T90-01",), in_flight=None):
        record = Workflow.start(Events.request(), "RUN-test")
        record.state, record.processed_event_ids, record.in_flight_event_id = state, list(processed), in_flight
        return record

    @staticmethod
    def blank():
        return Workflow.start(Events.request(), "RUN-test")

    @staticmethod
    def plan(on_approve=(Calls.coverage(),), on_reject=()):
        return AuthorizationPlan(summary="Raise limit", outcome="updated", on_approve=on_approve, on_reject=on_reject)


class TestAdmission:
    @pytest.mark.parametrize("event, record, fresh, verdict, rejection", [
        (Events.request(), None, False, Verdict.ADMIT, None),
        (Events.reply(), None, False, Verdict.REJECT, Rejection.UNKNOWN_TICKET),
        (Events.decision(), None, False, Verdict.REJECT, Rejection.UNKNOWN_TICKET),
        (Events.reply(), Records.flow(State.AWAITING_CLARIFICATION), False, Verdict.ADMIT, None),
        (Events.decision(), Records.flow(State.AWAITING_AUTHORIZATION), False, Verdict.ADMIT, None),
        (Events.request(), Records.flow(State.COMPLETED), False, Verdict.IGNORE, Rejection.DUPLICATE_EVENT),
        (Events.request(), Records.flow(State.COMPLETED), True, Verdict.ADMIT, None),
        (Events.request(event_id="EV-NEW"), Records.flow(State.COMPLETED), False, Verdict.REJECT, Rejection.TERMINAL),
        (Events.request(event_id="EV-NEW"), Records.flow(State.AWAITING_CLARIFICATION), False, Verdict.REJECT,
         Rejection.TICKET_ACTIVE),
        (Events.request(event_id="EV-NEW"), Records.flow(State.AWAITING_CLARIFICATION), True, Verdict.REJECT,
         Rejection.TICKET_ACTIVE),
        (Events.decision(), Records.flow(State.AWAITING_CLARIFICATION), False, Verdict.REJECT,
         Rejection.UNEXPECTED_EVENT),
        (Events.reply(), Records.flow(State.AWAITING_AUTHORIZATION), False, Verdict.REJECT,
         Rejection.UNEXPECTED_EVENT),
        (Events.reply(), Records.flow(State.COMPLETED), False, Verdict.REJECT, Rejection.TERMINAL),
        (Events.decision(), Records.flow(State.HANDOVER), False, Verdict.REJECT, Rejection.TERMINAL),
        (Events.reply(), Records.flow(State.GATHERING), False, Verdict.REJECT, Rejection.IN_FLIGHT),
        (Events.reply(), Records.flow(State.AWAITING_CLARIFICATION, in_flight="EV-OTHER"), False, Verdict.REJECT,
         Rejection.IN_FLIGHT),
        (Events.reply(), Records.flow(State.GATHERING, in_flight="EV-T90-02"), False, Verdict.RECOVER, None),
        (Events.reply(), Records.flow(State.COMPLETED, processed=("EV-T90-01", "EV-T90-02")), False, Verdict.IGNORE,
         Rejection.DUPLICATE_EVENT),
    ])
    def test_admission(self, event, record, fresh, verdict, rejection):
        admission = AdmissionPolicy(fresh).admit(event, record)
        assert (admission.verdict, admission.rejection) == (verdict, rejection)

    def test_admission_never_mutates_the_workflow(self):
        record = Records.flow(State.AWAITING_CLARIFICATION)
        before = record.model_dump()
        AdmissionPolicy().admit(Events.decision(), record)
        assert record.model_dump() == before


class TestProposal:
    def test_freeze_binds_references_and_hashes_both_branches(self):
        record, ask = Records.blank(), Decisions.claim_over_threshold()
        proposal = Proposal.freeze(record, ask.authorization, [{"ok": True, "data": {"claim_id": "CL-1"}}])
        assert proposal.on_approve[0].args["claim_id"] == "CL-1" and proposal.on_reject[0].args["claim_id"] == "CL-1"
        assert proposal.verify() and proposal.proposal_id == "PROP-T90-1"

    @pytest.mark.parametrize("branch", ["on_approve", "on_reject"])
    def test_tampering_with_either_branch_breaks_the_hash(self, branch):
        proposal = Proposal.freeze(Records.blank(), Records.plan(on_reject=(Calls.phone(),)), [])
        calls = getattr(proposal, branch)
        tampered = calls[0].model_copy(update={"args": {**calls[0].args, "changes": {"phone": "+390000000000"}}})
        setattr(proposal, branch, (tampered,))
        assert not proposal.verify()

    def test_hash_is_independent_of_key_order(self):
        a = Call(tool="update_coverage",
                 args={"policy_id": POLICY, "changes": {"annual_limit": "10000.00", "deductible": "500.00"}})
        b = Call(tool="update_coverage",
                 args={"changes": {"deductible": "500.00", "annual_limit": "10000.00"}, "policy_id": POLICY})
        assert CallCheck.digest((a,)) == CallCheck.digest((b,))

    @pytest.mark.parametrize("bad, message", [
        (Records.plan(on_approve=()), "at least one call"),
        (Records.plan(on_approve=(Call(tool="get_policy", args={"policy_id": POLICY}),)), "not a write tool"),
        (Records.plan(on_approve=(Call(tool="terminate_policy", args={"policy_id": "POL-002"}),)), "own policy"),
        (Records.plan(on_approve=(Calls.coverage("7000.00"),)), "invalid"),
        (Records.plan(on_approve=(Call(tool="update_coverage", args={"policy_id": POLICY, "changes": {}}),)),
         "invalid"),
        (Records.plan(on_reject=(Call(tool="terminate_policy", args={"policy_id": POLICY, "extra": 1}),)),
         "invalid"),
    ])
    def test_freeze_refuses_untrustworthy_calls(self, bad, message):
        with pytest.raises(ProposalError, match=message):
            Proposal.freeze(Records.blank(), bad, [])

    def test_only_one_proposal_per_ticket(self):
        record = Records.blank()
        record.proposal = Proposal.freeze(record, Records.plan(), [])
        with pytest.raises(ProposalError, match="Only one"):
            Proposal.freeze(record, Records.plan(), [])

    def test_rejection_reason_fills_only_calls_with_a_reason(self):
        record = Records.blank()
        proposal = Proposal.freeze(record, Decisions.claim_over_threshold().authorization,
                                   [{"ok": True, "data": {"claim_id": "C"}}])
        assert proposal.rejection_calls("Too expensive")[0].args["reason"] == "Too expensive"
        terminate = Call(tool="terminate_policy", args={"policy_id": POLICY})
        plain = Proposal.freeze(Records.blank(), Records.plan(on_reject=(terminate,)), [])
        assert "reason" not in plain.rejection_calls("No")[0].args

    def test_proposal_round_trips_through_json(self):
        proposal = Proposal.freeze(Records.blank(), Records.plan(on_reject=(Calls.phone(),)), [])
        assert Proposal.model_validate_json(proposal.model_dump_json()) == proposal

    def test_outcome_is_covered_by_the_hash(self):
        proposal = Proposal.freeze(Records.blank(), Records.plan(), [])
        proposal.outcome = "paid"
        assert not proposal.verify()


class TestRecordAndStore:
    def test_original_facts_are_frozen_and_conflicts_noted(self):
        record = Records.blank()
        record.merge_facts({"amount": "180.00", "postal_code": None})
        record.merge_facts({"amount": "999.00", "postal_code": "10100"})
        assert record.facts == {"amount": "180.00", "postal_code": "10100"}
        assert len(record.notes) == 1 and "amount" in record.notes[0]
        record.merge_facts({"amount": "999.00"})
        assert len(record.notes) == 1  # Same conflict is noted once.

    def test_reroute_reset_keeps_the_ticket_but_clears_classification(self):
        record = Records.blank()
        record.handling_agent, record.request_category = "claims", "out_of_scope_administration"
        record.facts = {"a": 1}
        record.reset_for_reroute("administration")
        assert (record.handling_agent, record.request_category, record.facts, record.reroutes) == (
            "administration", None, {}, 1)

    def test_workflow_round_trips_with_decision_and_proposal(self, tmp_path):
        store, record = WorkflowStore(tmp_path), Records.blank()
        record.decision = Decisions.claim_over_threshold()
        record.proposal = Proposal.freeze(record, record.decision.authorization,
                                          [{"ok": True, "data": {"claim_id": "C"}}])
        store.save(record)
        loaded = store.load("T90")
        assert loaded.decision == record.decision and loaded.proposal == record.proposal
        assert isinstance(loaded.decision, Decision) and loaded.state == State.RECEIVED and loaded.updated_at

    @pytest.mark.parametrize("ticket", ["../escape", "a/b", "", "x" * 101, "T 1"])
    def test_store_refuses_unsafe_ticket_ids(self, tmp_path, ticket):
        with pytest.raises(ValueError):
            WorkflowStore(tmp_path).path(ticket)

    def test_store_returns_none_for_unknown_ticket(self, tmp_path):
        assert WorkflowStore(tmp_path).load("T404") is None
