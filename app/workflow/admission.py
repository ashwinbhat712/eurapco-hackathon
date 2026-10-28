"""Event admission. Checked before any transition; a rejected event never changes the workflow."""

from enum import StrEnum

from app.workflow.schema import ValueModel
from app.workflow.states import SUSPENDED, TERMINAL, EventType


class Verdict(StrEnum):
    ADMIT = "admit"
    RECOVER = "recover"  # The same event was interrupted mid-handling: continue from the saved state.
    IGNORE = "ignore"  # Already processed: return the current state, no effects.
    REJECT = "reject"


class Rejection(StrEnum):
    DUPLICATE_EVENT = "duplicate_event"
    UNKNOWN_TICKET = "unknown_ticket"
    TICKET_ACTIVE = "ticket_active"
    TERMINAL = "workflow_terminal"
    IN_FLIGHT = "another_event_in_flight"
    UNEXPECTED_EVENT = "unexpected_event_for_state"


class Admission(ValueModel):
    verdict: Verdict
    rejection: Rejection | None = None


class AdmissionPolicy:
    """Ordered rules; the first rule that matches decides. `fresh` is a development-only rerun switch."""

    def __init__(self, fresh=False):
        self.fresh = fresh
        self.rules = (
            (self._fresh_rerun, Admission(verdict=Verdict.ADMIT)),
            (self._new_request, Admission(verdict=Verdict.ADMIT)),
            (self._unknown_ticket, Admission(verdict=Verdict.REJECT, rejection=Rejection.UNKNOWN_TICKET)),
            (self._already_processed, Admission(verdict=Verdict.IGNORE, rejection=Rejection.DUPLICATE_EVENT)),
            (self._interrupted, Admission(verdict=Verdict.RECOVER)),
            (self._terminal, Admission(verdict=Verdict.REJECT, rejection=Rejection.TERMINAL)),
            (self._request_on_active, Admission(verdict=Verdict.REJECT, rejection=Rejection.TICKET_ACTIVE)),
            (self._busy, Admission(verdict=Verdict.REJECT, rejection=Rejection.IN_FLIGHT)),
            (self._wrong_type, Admission(verdict=Verdict.REJECT, rejection=Rejection.UNEXPECTED_EVENT)),
        )

    def admit(self, event, workflow):
        return next((admission for rule, admission in self.rules if rule(event, workflow)),
                    Admission(verdict=Verdict.ADMIT))

    @staticmethod
    def _is_request(event):
        return event["event_type"] == EventType.REQUEST

    def _fresh_rerun(self, event, workflow):
        return self.fresh and self._is_request(event) and workflow is not None and workflow.state in TERMINAL

    def _new_request(self, event, workflow):
        return workflow is None and self._is_request(event)

    @staticmethod
    def _unknown_ticket(event, workflow):
        return workflow is None

    @staticmethod
    def _already_processed(event, workflow):
        return event["event_id"] in workflow.processed_event_ids

    @staticmethod
    def _interrupted(event, workflow):
        return event["event_id"] == workflow.in_flight_event_id

    @staticmethod
    def _terminal(event, workflow):
        return workflow.state in TERMINAL

    def _request_on_active(self, event, workflow):
        return self._is_request(event)

    @staticmethod
    def _busy(event, workflow):
        return workflow.in_flight_event_id is not None or workflow.state not in SUSPENDED

    @staticmethod
    def _wrong_type(event, workflow):
        return SUSPENDED[workflow.state] != event["event_type"]
