"""Internal workflow states, their mapping to the six logged agent states, and the transition table.

The table is the single source of truth: `StateMachine.transition()` refuses any move not listed here.
"""

from datetime import timedelta
from enum import StrEnum


class State(StrEnum):
    RECEIVED = "RECEIVED"
    ROUTED = "ROUTED"
    GATHERING = "GATHERING"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    EVALUATING = "EVALUATING"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    HANDOVER = "HANDOVER"
    FAILED = "FAILED"


class AgentState(StrEnum):
    PROCESSING = "processing"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    COMPLETED = "completed"
    CLOSED = "closed_without_execution"
    FAILED = "failed"


class Outcome(StrEnum):
    IN_PROGRESS = "in_progress"
    INFORMATION_REQUESTED = "information_requested"
    AUTHORIZATION_REQUESTED = "authorization_requested"
    ANSWERED = "answered"
    UPDATED = "updated"
    TERMINATED = "terminated"
    PAID = "paid"
    NO_CHANGE = "no_change"
    NOT_COVERED = "not_covered"
    INACTIVE_POLICY = "inactive_policy"
    OUTSIDE_COVERAGE_PERIOD = "outside_coverage_period"
    NOT_INSURED = "not_insured"
    DUPLICATE = "duplicate"
    INVALID_INPUT = "invalid_input"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    INSURER_REJECTED = "insurer_rejected"
    OUT_OF_SCOPE = "out_of_scope"
    UNCLEAR = "unclear"
    ZERO_REIMBURSEMENT = "zero_reimbursement"
    HANDOVER_REQUIRED = "handover_required"
    TECHNICAL_FAILURE = "technical_failure"


class EventType(StrEnum):
    REQUEST = "request"
    CLARIFICATION_REPLY = "clarification_reply"
    INSURER_DECISION = "insurer_decision"


class Agent(StrEnum):
    ADMINISTRATION = "administration"
    CLAIMS = "claims"


PROCESSING_STATES = frozenset({State.RECEIVED, State.ROUTED, State.GATHERING, State.EVALUATING, State.EXECUTING})
TERMINAL = frozenset({State.COMPLETED, State.CLOSED, State.HANDOVER, State.FAILED})
# A suspended state accepts exactly one event type.
SUSPENDED = {State.AWAITING_CLARIFICATION: EventType.CLARIFICATION_REPLY,
             State.AWAITING_AUTHORIZATION: EventType.INSURER_DECISION}

AGENT_STATE = {
    **{state: AgentState.PROCESSING for state in PROCESSING_STATES},
    State.AWAITING_CLARIFICATION: AgentState.AWAITING_CLARIFICATION,
    State.AWAITING_AUTHORIZATION: AgentState.AWAITING_AUTHORIZATION,
    State.COMPLETED: AgentState.COMPLETED,
    State.CLOSED: AgentState.CLOSED,
    State.HANDOVER: AgentState.CLOSED,  # Plan §15 decision 2: confirm with the organisers.
    State.FAILED: AgentState.FAILED,
}

# Plan §15 decision 1: zero reimbursement is CLOSED (the requested effect, a payment, did not happen).
OUTCOMES = {
    **{state: frozenset({Outcome.IN_PROGRESS}) for state in PROCESSING_STATES},
    State.AWAITING_CLARIFICATION: frozenset({Outcome.INFORMATION_REQUESTED}),
    State.AWAITING_AUTHORIZATION: frozenset({Outcome.AUTHORIZATION_REQUESTED}),
    State.COMPLETED: frozenset({Outcome.ANSWERED, Outcome.UPDATED, Outcome.TERMINATED, Outcome.PAID,
                                Outcome.NO_CHANGE}),
    State.CLOSED: frozenset({Outcome.NOT_COVERED, Outcome.INACTIVE_POLICY, Outcome.OUTSIDE_COVERAGE_PERIOD,
                             Outcome.NOT_INSURED, Outcome.DUPLICATE, Outcome.INVALID_INPUT,
                             Outcome.INSUFFICIENT_INFORMATION, Outcome.INSURER_REJECTED, Outcome.OUT_OF_SCOPE,
                             Outcome.UNCLEAR, Outcome.ZERO_REIMBURSEMENT}),
    State.HANDOVER: frozenset({Outcome.HANDOVER_REQUIRED}),
    State.FAILED: frozenset({Outcome.TECHNICAL_FAILURE}),
}

_FORWARD = {
    State.RECEIVED: {State.ROUTED},
    # The router never closes: a supported request must not be rejected because the first guess was wrong.
    State.ROUTED: {State.GATHERING},
    # ROUTED again = the single reroute to the other agent.
    State.GATHERING: {State.ROUTED, State.AWAITING_CLARIFICATION, State.CLOSED, State.EVALUATING},
    # CLOSED = clarification timeout.
    State.AWAITING_CLARIFICATION: {State.GATHERING, State.CLOSED},
    # Harness-built writes before the proposal (claims lifecycle) run in EVALUATING and can close or hand over.
    State.EVALUATING: {State.COMPLETED, State.CLOSED, State.AWAITING_AUTHORIZATION, State.EXECUTING, State.HANDOVER},
    # CLOSED = reject (stored on_reject runs here). HANDOVER = timeout or a failed stored call.
    State.AWAITING_AUTHORIZATION: {State.EXECUTING, State.CLOSED, State.HANDOVER},
    State.EXECUTING: {State.COMPLETED, State.CLOSED, State.HANDOVER},
}
# Any non-terminal state may fail; terminal states never move again.
TRANSITIONS = {state: frozenset({*_FORWARD.get(state, ()), *({State.FAILED} if state not in TERMINAL else ())})
               for state in State}

# A suspended ticket expires when its next event arrives too late. Measured on event timestamps, so it is
# deterministic and replayable. Team choice: the challenge does not prescribe a timeout.
TIMEOUTS = {
    State.AWAITING_CLARIFICATION: (timedelta(days=14), State.CLOSED, Outcome.INSUFFICIENT_INFORMATION),
    State.AWAITING_AUTHORIZATION: (timedelta(days=7), State.HANDOVER, Outcome.HANDOVER_REQUIRED),
}

# Categories that end the ticket without asking. Cross-domain categories reroute instead (max once).
CLOSING_CATEGORIES = {
    "unclear": Outcome.UNCLEAR,
    "out_of_scope_iban": Outcome.OUT_OF_SCOPE,
    "out_of_scope_renewal": Outcome.OUT_OF_SCOPE,
    "out_of_scope_other": Outcome.OUT_OF_SCOPE,
    "out_of_scope_claim_integration": Outcome.OUT_OF_SCOPE,
}
REROUTE_TARGET = {"out_of_scope_claims": Agent.CLAIMS, "out_of_scope_administration": Agent.ADMINISTRATION}
# The category an agent uses to hand a request over to `agent`.
HANDOFF_CATEGORY = {agent: category for category, agent in REROUTE_TARGET.items()}
MAX_REROUTES = 1
