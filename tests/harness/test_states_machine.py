"""Unit tests: the transition table, its mapping to the common vocabulary, and the state machine."""

from datetime import datetime, timedelta

import pytest

from app.contracts import document
from app.workflow.machine import IllegalTransition, StateMachine
from app.workflow.record import Transition, Workflow
from app.workflow.states import (AGENT_STATE, CLOSING_CATEGORIES, OUTCOMES, PROCESSING_STATES, REROUTE_TARGET,
                                 SUSPENDED, TERMINAL, TIMEOUTS, TRANSITIONS, Outcome, State)
from harness.fakes import Events

VOCABULARY = document("vocabulary.json")


class SpyLog:
    def __init__(self):
        self.statuses = []

    def status(self, **kwargs):
        self.statuses.append(kwargs)


class Records:
    @staticmethod
    def workflow(state=State.RECEIVED, agent="administration", category="contact_update"):
        record = Workflow.start(Events.request(), "RUN-test")
        record.state, record.handling_agent, record.request_category = state, agent, category
        return record

    @staticmethod
    def plus(moment, delta):
        return (moment + delta).isoformat()


class TestVocabulary:
    def test_every_state_maps_to_a_common_agent_state(self):
        assert set(AGENT_STATE) == set(State)
        assert {str(value) for value in AGENT_STATE.values()} <= set(VOCABULARY["agent_states"])

    def test_every_outcome_is_in_the_common_vocabulary(self):
        assert {str(outcome) for outcome in Outcome} == set(VOCABULARY["request_outcomes"])
        assert set(OUTCOMES) == set(State)

    def test_each_outcome_belongs_to_exactly_one_final_state_family(self):
        finals = [outcome for state in TERMINAL for outcome in OUTCOMES[state]]
        assert len(finals) == len(set(finals))

    def test_category_tables_use_vocabulary_values(self):
        categories = {label for labels in VOCABULARY["request_categories"].values() for label in labels}
        assert set(CLOSING_CATEGORIES) | set(REROUTE_TARGET) <= categories
        assert REROUTE_TARGET["out_of_scope_claims"] == "claims"
        assert REROUTE_TARGET["out_of_scope_administration"] == "administration"


class TestTable:
    @pytest.mark.parametrize("state", sorted(TERMINAL))
    def test_terminal_states_never_move(self, state):
        assert TRANSITIONS[state] == frozenset()

    @pytest.mark.parametrize("state", sorted(set(State) - TERMINAL))
    def test_every_non_terminal_state_can_fail(self, state):
        assert State.FAILED in TRANSITIONS[state]

    def test_every_non_terminal_state_reaches_a_terminal_state(self):
        for start in set(State) - TERMINAL:
            seen, frontier = set(), {start}
            while frontier:
                seen |= frontier
                frontier = {target for state in frontier for target in TRANSITIONS[state]} - seen
            assert seen & {State.COMPLETED, State.CLOSED}, start

    def test_suspended_states_only_resume_forward(self):
        assert TRANSITIONS[State.AWAITING_CLARIFICATION] >= {State.GATHERING}
        assert State.EVALUATING not in TRANSITIONS[State.AWAITING_CLARIFICATION]  # Facts must be re-checked first.
        assert State.COMPLETED not in TRANSITIONS[State.AWAITING_AUTHORIZATION]  # Approval must execute.

    def test_handover_is_logged_as_closed_without_execution(self):
        assert AGENT_STATE[State.HANDOVER] == "closed_without_execution"
        assert OUTCOMES[State.HANDOVER] == {Outcome.HANDOVER_REQUIRED}

    def test_zero_reimbursement_is_a_closure(self):
        assert Outcome.ZERO_REIMBURSEMENT in OUTCOMES[State.CLOSED]


class TestMachine:
    def test_transition_updates_record_history_and_log(self):
        log, record = SpyLog(), Records.workflow(State.EVALUATING)
        StateMachine(log).transition(record, State.COMPLETED, outcome=Outcome.UPDATED, reason="ok", message="Done")
        assert (record.state, record.outcome, record.message) == (State.COMPLETED, Outcome.UPDATED, "Done")
        assert record.history[-1] == Transition(source=State.EVALUATING, target=State.COMPLETED,
                                                outcome=Outcome.UPDATED, reason="ok")
        assert log.statuses[-1]["agent_state"] == "completed"
        assert log.statuses[-1]["request_category"] == "contact_update"

    def test_illegal_transition_changes_nothing(self):
        log, record = SpyLog(), Records.workflow(State.RECEIVED)
        with pytest.raises(IllegalTransition):
            StateMachine(log).transition(record, State.EXECUTING)
        assert record.state == State.RECEIVED and not record.history and not log.statuses

    @pytest.mark.parametrize("target, outcome", [(State.COMPLETED, Outcome.INSURER_REJECTED),
                                                 (State.CLOSED, Outcome.PAID), (State.COMPLETED, None),
                                                 (State.CLOSED, "not-a-value")])
    def test_outcome_must_fit_the_target_state(self, target, outcome):
        with pytest.raises((IllegalTransition, ValueError)):
            StateMachine().transition(Records.workflow(State.EVALUATING), target, outcome=outcome)

    def test_single_outcome_states_default_their_outcome(self):
        record = Records.workflow(State.GATHERING)
        StateMachine().transition(record, State.AWAITING_CLARIFICATION, at="2026-10-22T10:00:00+02:00")
        assert record.outcome == Outcome.INFORMATION_REQUESTED
        assert record.suspended_at == datetime.fromisoformat("2026-10-22T10:00:00+02:00")

    @pytest.mark.parametrize("agent, category", [(None, "contact_update"), ("administration", None)])
    def test_classified_states_require_agent_and_category(self, agent, category):
        record = Records.workflow(State.ROUTED, agent, category)
        with pytest.raises(IllegalTransition):
            StateMachine().transition(record, State.CLOSED, outcome=Outcome.UNCLEAR)
        assert record.state == State.ROUTED

    def test_failed_needs_no_classification(self):
        record = Records.workflow(State.RECEIVED, None, None)
        StateMachine().transition(record, State.FAILED)
        assert record.outcome == Outcome.TECHNICAL_FAILURE


class TestTimeout:
    @pytest.mark.parametrize("state", sorted(SUSPENDED))
    def test_timeout_boundary_is_inclusive_of_the_limit(self, state):
        limit, target, outcome = TIMEOUTS[state]
        record = Records.workflow(state)
        record.suspended_at = "2026-10-01T10:00:00+00:00"
        machine = StateMachine()
        assert not machine.expire_if_due(record, Records.plus(record.suspended_at, limit))
        assert machine.expire_if_due(record, Records.plus(record.suspended_at, limit + timedelta(seconds=1)))
        assert (record.state, record.outcome) == (target, outcome)

    def test_timeout_compares_across_timezones(self):
        record = Records.workflow(State.AWAITING_CLARIFICATION)
        record.suspended_at = "2026-10-01T10:00:00+02:00"
        assert not StateMachine().expire_if_due(record, "2026-10-15T08:00:00+00:00")  # Exactly 14 days.

    def test_processing_states_never_time_out(self):
        for state in PROCESSING_STATES:
            record = Records.workflow(state)
            record.suspended_at = "2020-01-01T00:00:00+00:00"
            assert not StateMachine().expire_if_due(record, "2026-01-01T00:00:00+00:00")
