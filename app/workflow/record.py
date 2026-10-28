"""The persisted workflow record: everything needed to resume a ticket in the same run.

`Workflow.model_json_schema()` is the schema of .runtime/workflows/<ticket_id>.json.
"""

from enum import StrEnum
from typing import Any, ClassVar, Literal, Self

from pydantic import AwareDatetime, Field, NonNegativeInt, model_validator

from app.workflow.facts import facts_model
from app.workflow.ports import Decision, GatherContext
from app.workflow.proposal import Proposal
from app.workflow.schema import Category, SafeId, Sha256, StateModel
from app.workflow.states import MAX_REROUTES, Agent, Outcome, State


class LedgerStatus(StrEnum):
    PENDING = "pending"  # Saved BEFORE the call: after a crash the effect is unknown, not absent.
    DONE = "done"
    FAILED = "failed"


class Clarification(StateModel):
    """The one question per ticket and, once it arrives, the customer's reply."""

    question: str = Field(min_length=1)  # One question listing every missing item.
    missing: list[str] = Field(min_length=1)
    answered: bool = False
    reply: dict[str, str] | None = None


class LedgerEntry(StateModel):
    """Write-ahead entry: saved as PENDING before the domain call, settled after it."""

    tool: str
    args: dict[str, Any]
    hash: Sha256
    status: LedgerStatus = LedgerStatus.PENDING
    result: dict[str, Any] | None = None


class Transition(StateModel):
    source: State
    target: State
    outcome: Outcome
    reason: str


class Workflow(StateModel):
    schema_version: Literal[1] = 1
    ticket_id: SafeId
    run_id: SafeId
    policy_id: str
    original_request: dict[str, Any]
    state: State = State.RECEIVED
    outcome: Outcome = Outcome.IN_PROGRESS
    reason: str = ""
    message: str = ""
    handling_agent: Agent | None = None
    request_category: Category | None = None
    category_hint: Category | None = None
    processed_event_ids: list[str] = Field(default_factory=list)
    in_flight_event_id: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)  # Keys and types per request_category: facts.py.
    notes: list[str] = Field(default_factory=list)
    other_requests: list[str] = Field(default_factory=list)  # Parts of a mixed message this ticket does not handle.
    clarification: Clarification | None = None
    decision: Decision | None = None
    proposal: Proposal | None = None
    write_ledger: list[LedgerEntry] = Field(default_factory=list)
    correction_attempts: dict[str, NonNegativeInt] = Field(default_factory=dict)
    reroutes: int = Field(default=0, ge=0, le=MAX_REROUTES)
    history: list[Transition] = Field(default_factory=list)
    suspended_at: AwareDatetime | None = None  # Timestamp of the event that caused the suspension.
    updated_at: AwareDatetime | None = None

    _EMPTY: ClassVar[tuple] = (None, "", [], {})

    @model_validator(mode="after")
    def _facts_fit_category(self) -> Self:
        """Runs on load and on every assignment (validate_assignment)."""
        if self.request_category is not None:
            facts_model(self.request_category).model_validate(self.facts)
        return self

    @classmethod
    def start(cls, request, run_id):
        return cls(ticket_id=request["ticket_id"], run_id=run_id, policy_id=request["policy_id"],
                   original_request=request, in_flight_event_id=request["event_id"])

    @property
    def clarification_asked(self):
        return self.clarification is not None

    def begin_event(self, event_id):
        self.in_flight_event_id = event_id

    def finish_event(self):
        if self.in_flight_event_id and self.in_flight_event_id not in self.processed_event_ids:
            self.processed_event_ids.append(self.in_flight_event_id)
        self.in_flight_event_id = None

    def gather_context(self):
        return GatherContext(original_request=self.original_request, facts=dict(self.facts),
                             question=getattr(self.clarification, "question", None),
                             reply=getattr(self.clarification, "reply", None))

    def merge_facts(self, new_facts):
        """Facts already supplied are frozen: a later message can fill gaps, never overwrite."""
        frozen = {key: value for key, value in self.facts.items() if value not in self._EMPTY}
        conflicts = sorted(key for key, value in new_facts.items()
                           if key in frozen and value not in self._EMPTY and value != frozen[key])
        self.facts = {**new_facts, **frozen}
        self.notes.extend(note for note in (f"Kept the original {key}; a later message tried to change it."
                                            for key in conflicts) if note not in self.notes)

    def note_other_requests(self, requests):
        new = [request for request in requests if request not in self.other_requests]
        self.other_requests.extend(new)
        self.notes.extend(f"Not handled, to be sent separately: {request}." for request in new)

    @property
    def typed_facts(self):
        """The facts as the category's model (app/workflow/facts.py), for rules that want attributes."""
        return facts_model(self.request_category).model_validate(self.facts) if self.request_category else None

    def classify(self, category):
        """Set the category; facts the new category does not know are dropped first, so they never mix."""
        known = facts_model(category).model_fields
        self.facts = {key: value for key, value in self.facts.items() if key in known}
        self.request_category = category

    def reset_for_reroute(self, agent):
        self.reroutes += 1
        self.handling_agent, self.request_category, self.facts = agent, None, {}
