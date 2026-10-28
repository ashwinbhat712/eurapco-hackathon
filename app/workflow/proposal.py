"""The single frozen proposal per ticket. On approval exactly the stored calls run; the model is not asked again."""

import hashlib
import json
from enum import StrEnum
from typing import ClassVar

from pydantic import Field

from app.contracts import TOOLS, validator
from app.workflow.ports import Call
from app.workflow.schema import Sha256, StateModel
from app.workflow.states import Outcome

WRITE_TOOLS = frozenset({"update_contact_details", "update_coverage", "terminate_policy",
                         "create_claim", "pay_claim", "record_claim_outcome"})


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


class ProposalError(ValueError):
    """The proposal cannot be frozen or trusted. Nothing is sent to the insurer or executed."""


class CallCheck:
    """Canonical hashing and pre-execution validation of harness-built calls."""

    RUN_ID_PLACEHOLDER = "RUN-call-check"

    @staticmethod
    def sha256(value):
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def digest(cls, calls):
        return cls.sha256([call.model_dump(mode="json") for call in calls])

    @classmethod
    def validate(cls, call, policy_id):
        if call.tool not in WRITE_TOOLS:
            raise ProposalError(f"{call.tool} is not a write tool.")
        if call.args.get("policy_id") != policy_id:
            raise ProposalError("A call may only act on the ticket's own policy.")
        payload = {**call.args, "run_id": cls.RUN_ID_PLACEHOLDER}
        errors = list(validator("tools.schema.json", TOOLS[call.tool][0] + "Input").iter_errors(payload))
        if errors:
            raise ProposalError(f"{call.tool} arguments are invalid: {errors[0].message}")


class Proposal(StateModel):
    HASHED_FIELDS: ClassVar[frozenset[str]] = frozenset({"summary", "outcome", "on_approve", "on_reject"})

    proposal_id: str
    summary: str
    outcome: Outcome
    on_approve: tuple[Call, ...] = Field(min_length=1)
    on_reject: tuple[Call, ...] = ()
    hash: Sha256
    status: ProposalStatus = ProposalStatus.PENDING
    decision_reason: str | None = None

    @classmethod
    def freeze(cls, workflow, plan, prior_results):
        """Bind references to the prep results, validate every call, hash both branches."""
        if workflow.proposal is not None:
            raise ProposalError("Only one proposal per ticket.")
        if not plan.on_approve:
            raise ProposalError("A proposal needs at least one call to execute on approval.")
        on_approve = tuple(call.bind(prior_results) for call in plan.on_approve)
        on_reject = tuple(call.bind(prior_results) for call in plan.on_reject)
        for call in (*on_approve, *on_reject):
            CallCheck.validate(call, workflow.policy_id)
        content = {"summary": plan.summary, "outcome": plan.outcome, "on_approve": on_approve, "on_reject": on_reject}
        return cls(proposal_id=f"PROP-{workflow.ticket_id}-1", **content, hash=cls.content_hash(**content))

    @staticmethod
    def content_hash(summary, outcome, on_approve, on_reject):
        """Each branch is hashed under its own key, so moving a call between branches changes the hash."""
        return CallCheck.sha256({"summary": summary, "outcome": str(outcome),
                                 "on_approve": [call.model_dump(mode="json") for call in on_approve],
                                 "on_reject": [call.model_dump(mode="json") for call in on_reject]})

    def verify(self):
        return self.content_hash(**{name: getattr(self, name) for name in self.HASHED_FIELDS}) == self.hash

    def rejection_calls(self, reason):
        """Fill the insurer's reason into stored on_reject calls that carry a reason argument."""
        return tuple(call.model_copy(update={"args": {**call.args, "reason": reason}}) if "reason" in call.args
                     else call for call in self.on_reject)
