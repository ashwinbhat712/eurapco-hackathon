"""The only place a workflow changes state. Every move is checked against the table and logged."""

from datetime import datetime

from app.workflow.messages import final_message
from app.workflow.record import Transition
from app.workflow.states import AGENT_STATE, OUTCOMES, SUSPENDED, TERMINAL, TIMEOUTS, TRANSITIONS, AgentState, Outcome


class IllegalTransition(ValueError):
    """The harness tried a move that the transition table does not allow. Nothing was changed."""


class StateMachine:
    UNCLASSIFIED_ALLOWED = frozenset({AgentState.PROCESSING, AgentState.FAILED})

    def __init__(self, log=None):
        self.log = log

    @staticmethod
    def allowed(source, target):
        return target in TRANSITIONS[source]

    def transition(self, workflow, target, *, outcome=None, reason="", message="", at=None):
        source = workflow.state
        if not self.allowed(source, target):
            raise IllegalTransition(f"{source} -> {target} is not allowed.")
        outcome = self._outcome(target, outcome)
        agent_state = AGENT_STATE[target]
        if agent_state not in self.UNCLASSIFIED_ALLOWED and not (workflow.handling_agent and workflow.request_category):
            raise IllegalTransition(f"{target} requires handling_agent and request_category.")
        reason = reason or f"{source} -> {target}"
        workflow.history.append(Transition(source=source, target=target, outcome=outcome, reason=reason))
        workflow.state, workflow.outcome, workflow.reason = target, outcome, reason
        workflow.message = final_message(workflow, outcome, message) if target in TERMINAL else message or workflow.message
        if target in SUSPENDED:
            workflow.suspended_at = at
        if self.log is not None:
            self.log.status(agent_state=agent_state, request_outcome=outcome, reason=reason, message=workflow.message,
                            request_category=workflow.request_category)

    def expire_if_due(self, workflow, event_time):
        """Close a suspended workflow whose next event arrives after the timeout. Returns True if it expired."""
        rule = TIMEOUTS.get(workflow.state)
        if rule is None or workflow.suspended_at is None:
            return False
        limit, target, outcome = rule
        waited = datetime.fromisoformat(event_time) - workflow.suspended_at
        if waited <= limit:
            return False
        awaited = SUSPENDED[workflow.state]
        self.transition(workflow, target, outcome=outcome, reason=f"No {awaited} within {limit}; waited {waited}.")
        return True

    @staticmethod
    def _outcome(target, outcome):
        allowed = OUTCOMES[target]
        if outcome is None and len(allowed) == 1:
            return next(iter(allowed))
        if outcome is None or Outcome(outcome) not in allowed:
            raise IllegalTransition(f"Outcome {outcome!r} is not allowed in {target}.")
        return Outcome(outcome)
