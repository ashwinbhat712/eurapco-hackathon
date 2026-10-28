"""State machine: allowed/forbidden transitions, log mapping, classification gate and timeouts."""

from datetime import datetime

import pytest

from app.contracts import document
from app.evaluation import EvaluationLog
from app.workflow.machine import IllegalTransition, StateMachine
from app.workflow.record import Transition, Workflow
from app.workflow.states import (AGENT_STATE, OUTCOMES, SUSPENDED, TERMINAL, TIMEOUTS, TRANSITIONS, AgentState,
                                 Outcome, State)

REQUEST = {"event_type": "request", "event_id": "EV-1", "ticket_id": "T1", "policy_id": "POL-001",
           "author_name": "Taylor Example", "timestamp": "2026-10-22T10:00:00+02:00", "text": "Change my phone"}
SUSPENDED_AT = "2026-10-22T10:00:00+02:00"
ALLOWED = sorted((source, target) for source, targets in TRANSITIONS.items() for target in targets)
FORBIDDEN = sorted((source, target) for source in State for target in State if target not in TRANSITIONS[source])


class TestTable:
    def test_every_state_is_mapped_to_a_logged_agent_state_and_outcomes(self):
        assert set(AGENT_STATE) == set(State) == set(OUTCOMES)

    def test_logged_values_exist_in_the_shared_vocabulary(self):
        vocabulary = document("vocabulary.json")
        assert {str(state) for state in AgentState} <= set(vocabulary["agent_states"])
        assert {str(outcome) for outcome in Outcome} <= set(vocabulary["request_outcomes"])

    def test_terminal_states_never_move(self):
        assert all(TRANSITIONS[state] == frozenset() for state in TERMINAL)

    def test_every_non_terminal_state_can_fail(self):
        assert all(State.FAILED in TRANSITIONS[state] for state in State if state not in TERMINAL)

    def test_suspended_states_only_resume_or_end(self):
        assert TRANSITIONS[State.AWAITING_CLARIFICATION] == {State.GATHERING, State.CLOSED, State.FAILED}
        assert TRANSITIONS[State.AWAITING_AUTHORIZATION] == {State.EXECUTING, State.CLOSED, State.HANDOVER,
                                                            State.FAILED}

    def test_handover_is_logged_as_closed_without_execution(self):
        assert AGENT_STATE[State.HANDOVER] == AgentState.CLOSED
        assert OUTCOMES[State.HANDOVER] == {Outcome.HANDOVER_REQUIRED}

    def test_zero_reimbursement_is_a_closed_outcome(self):
        assert Outcome.ZERO_REIMBURSEMENT in OUTCOMES[State.CLOSED]
        assert Outcome.ZERO_REIMBURSEMENT not in OUTCOMES[State.COMPLETED]

    def test_only_suspended_states_have_timeouts(self):
        assert set(TIMEOUTS) == set(SUSPENDED)


class TestTransition:
    @staticmethod
    def workflow(state, classified=True):
        workflow = Workflow.start(REQUEST, "RUN-test")
        workflow.state = state
        if classified:
            workflow.handling_agent, workflow.request_category = "administration", "contact_update"
        return workflow

    @pytest.mark.parametrize("source, target", ALLOWED)
    def test_allowed_transition_moves_and_records_history(self, source, target):
        workflow = self.workflow(source)
        StateMachine().transition(workflow, target, outcome=next(iter(sorted(OUTCOMES[target]))), reason="test")
        assert workflow.state == target
        assert workflow.history[-1] == Transition(source=source, target=target, outcome=workflow.outcome,
                                                  reason="test")

    @pytest.mark.parametrize("source, target", FORBIDDEN)
    def test_forbidden_transition_raises_and_changes_nothing(self, source, target):
        workflow = self.workflow(source)
        before = workflow.model_dump()
        with pytest.raises(IllegalTransition, match="not allowed"):
            StateMachine().transition(workflow, target, outcome=next(iter(sorted(OUTCOMES[target]))))
        assert workflow.model_dump() == before

    @pytest.mark.parametrize("target, outcome", [
        (State.COMPLETED, Outcome.INSURER_REJECTED), (State.CLOSED, Outcome.PAID),
        (State.HANDOVER, Outcome.INSUFFICIENT_INFORMATION), (State.EVALUATING, Outcome.ANSWERED)])
    def test_outcome_must_belong_to_the_target_state(self, target, outcome):
        source = next(state for state in State if target in TRANSITIONS[state])
        with pytest.raises(IllegalTransition, match="Outcome"):
            StateMachine().transition(self.workflow(source), target, outcome=outcome)

    def test_terminal_state_with_several_outcomes_requires_one(self):
        with pytest.raises(IllegalTransition, match="Outcome None"):
            StateMachine().transition(self.workflow(State.EVALUATING), State.COMPLETED)

    def test_single_outcome_state_fills_the_outcome(self):
        workflow = self.workflow(State.EXECUTING)
        StateMachine().transition(workflow, State.HANDOVER)
        assert workflow.outcome == Outcome.HANDOVER_REQUIRED

    @pytest.mark.parametrize("source, target", [(State.GATHERING, State.AWAITING_CLARIFICATION),
                                                (State.EVALUATING, State.COMPLETED),
                                                (State.GATHERING, State.CLOSED)])
    def test_classification_required_outside_processing(self, source, target):
        workflow = self.workflow(source, classified=False)
        with pytest.raises(IllegalTransition, match="handling_agent and request_category"):
            StateMachine().transition(workflow, target, outcome=next(iter(sorted(OUTCOMES[target]))))
        assert workflow.state == source

    def test_failure_is_allowed_before_classification(self):
        workflow = self.workflow(State.RECEIVED, classified=False)
        StateMachine().transition(workflow, State.FAILED)
        assert workflow.outcome == Outcome.TECHNICAL_FAILURE

    def test_entering_a_suspended_state_records_the_event_time(self):
        workflow = self.workflow(State.GATHERING)
        StateMachine().transition(workflow, State.AWAITING_CLARIFICATION, at=SUSPENDED_AT)
        assert workflow.suspended_at == datetime.fromisoformat(SUSPENDED_AT)

    def test_transition_logs_the_mapped_agent_state(self, tmp_path):
        log = EvaluationLog(REQUEST, "RUN-test", "TEAM-1", tmp_path)
        log.route("administration")
        workflow = self.workflow(State.GATHERING)
        StateMachine(log).transition(workflow, State.AWAITING_CLARIFICATION, reason="Missing postal_code")
        assert log.record["agent_state"] == "awaiting_clarification"
        assert log.record["request_outcome"] == "information_requested"
        assert log.record["events"][-1]["data"]["reason"] == "Missing postal_code"


class TestTimeout:
    @staticmethod
    def suspended(state):
        workflow = TestTransition.workflow(state)
        workflow.suspended_at = SUSPENDED_AT
        return workflow

    @pytest.mark.parametrize("state, arrives", [
        (State.AWAITING_CLARIFICATION, "2026-11-05T10:00:00+02:00"),  # Exactly 14 days: still in time.
        (State.AWAITING_AUTHORIZATION, "2026-10-29T10:00:00+02:00"),  # Exactly 7 days.
        (State.AWAITING_CLARIFICATION, "2026-10-22T09:00:00+02:00"),  # Earlier than the request: not late.
    ])
    def test_in_time_event_does_not_expire(self, state, arrives):
        workflow = self.suspended(state)
        assert StateMachine().expire_if_due(workflow, arrives) is False
        assert workflow.state == state

    @pytest.mark.parametrize("state, arrives, target, outcome", [
        (State.AWAITING_CLARIFICATION, "2026-11-05T10:00:01+02:00", State.CLOSED, Outcome.INSUFFICIENT_INFORMATION),
        (State.AWAITING_AUTHORIZATION, "2026-10-29T10:00:01+02:00", State.HANDOVER, Outcome.HANDOVER_REQUIRED),
    ])
    def test_late_event_expires_the_workflow(self, state, arrives, target, outcome):
        workflow = self.suspended(state)
        assert StateMachine().expire_if_due(workflow, arrives) is True
        assert (workflow.state, workflow.outcome) == (target, outcome)

    def test_timezone_offsets_are_compared_as_instants(self):
        workflow = self.suspended(State.AWAITING_AUTHORIZATION)
        assert StateMachine().expire_if_due(workflow, "2026-10-29T08:00:00+00:00") is False  # = 10:00+02:00

    @pytest.mark.parametrize("state", [State.GATHERING, State.EXECUTING, State.COMPLETED])
    def test_states_without_timeout_never_expire(self, state):
        workflow = self.suspended(state)
        assert StateMachine().expire_if_due(workflow, "2030-01-01T00:00:00+00:00") is False
