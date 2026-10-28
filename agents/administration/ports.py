"""Thin interface between the administration rules and the shared workflow harness.

The workflow engine (stop/resume, event correlation, persistence) and the real
proposal store are built outside this package. Administration only depends on the
protocols below, so the harness can supply its own implementations. The in-memory
implementations here are reference ones, used by the tests and by local runs.
"""

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

PHASES = ("initial", "clarification_reply", "insurer_decision")


class ProposalIntegrityError(RuntimeError):
    """A stored proposal does not match its hash, so it must not be executed."""


def proposal_hash(tool, args):
    """Stable hash over the frozen call: canonical JSON, sorted keys, no whitespace."""
    payload = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_proposal(tool, args, **details):
    proposal = {"tool": tool, "args": copy.deepcopy(args), "hash": proposal_hash(tool, args)}
    proposal.update(copy.deepcopy(details))
    return proposal


def verify_proposal(proposal):
    """Return the stored call only when it still matches its own hash."""
    if not isinstance(proposal, dict) or not {"tool", "args", "hash"} <= set(proposal):
        raise ProposalIntegrityError("The stored proposal is not a {tool, args, hash} record.")
    if proposal_hash(proposal["tool"], proposal["args"]) != proposal["hash"]:
        raise ProposalIntegrityError("The stored proposal does not match its hash; it will not be executed.")
    return proposal["tool"], copy.deepcopy(proposal["args"])


class DomainCaller(Protocol):
    """app.mcp_client.DomainClient already satisfies this."""

    async def call(self, name: str, arguments: dict) -> dict: ...


class ProposalStore(Protocol):
    def save(self, ticket_id: str, proposal: dict) -> str: ...

    def load(self, ticket_id: str) -> Optional[dict]: ...


class Extractor(Protocol):
    """Turns free text into a validated AdminIntent. The model never decides effects."""

    async def extract(self, event: dict, workflow: "WorkflowState") -> Any: ...


@dataclass
class WorkflowState:
    """What the administration package needs to know about the suspended workflow."""

    ticket_id: str
    run_id: str
    policy_id: str
    phase: str = "initial"
    clarification_asked: bool = False
    facts: dict = field(default_factory=dict)
    tool_error_attempts: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.phase not in PHASES:
            raise ValueError(f"Unknown workflow phase: {self.phase}")

    @property
    def is_reply(self):
        return self.phase == "clarification_reply"

    def clarification_issued(self):
        self.clarification_asked = True

    def attempts_for(self, signature):
        return self.tool_error_attempts.get(signature, 0)

    def record_attempt(self, signature):
        self.tool_error_attempts[signature] = self.attempts_for(signature) + 1
        return self.tool_error_attempts[signature]


class InMemoryProposalStore:
    """Reference store: one proposal per ticket, integrity checked on save and load."""

    def __init__(self):
        self._proposals = {}

    def save(self, ticket_id, proposal):
        tool, args = verify_proposal(proposal)
        self._proposals[ticket_id] = copy.deepcopy(proposal)
        return proposal["hash"]

    def load(self, ticket_id):
        proposal = self._proposals.get(ticket_id)
        return copy.deepcopy(proposal) if proposal is not None else None
