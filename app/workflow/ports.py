"""Contracts between the Part 1 harness and the parts owned by others.

Router: first guess (someone else). Agents: model work inside a phase (architecture C).
Rules: minimum information and business decisions (Parts 2-4). The harness owns state, proposals and writes.
"""

from enum import StrEnum
from typing import Any, ClassVar, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from app.workflow.facts import facts_model
from app.workflow.schema import Category, ValueModel
from app.workflow.states import Agent, Outcome


class Call(ValueModel):
    """One write. Args may hold a reference {"$result": i, "$key": k} to an earlier result in the same batch."""

    tool: str = Field(min_length=1)
    args: dict[str, Any]

    REF: ClassVar[str] = "$result"
    KEY: ClassVar[str] = "$key"

    def bind(self, results):
        return self.model_copy(update={"args": self._resolve(self.args, results)})

    @classmethod
    def _resolve(cls, value, results):
        if isinstance(value, dict) and cls.REF in value:
            return results[value[cls.REF]]["data"][value[cls.KEY]]
        if isinstance(value, dict):
            return {key: cls._resolve(item, results) for key, item in value.items()}
        return [cls._resolve(item, results) for item in value] if isinstance(value, list) else value

    @classmethod
    def ref(cls, index, key):
        return {cls.REF: index, cls.KEY: key}


class RouteResult(ValueModel):
    agent: Agent
    category_hint: Category | None = None  # A hint only; never logged as the final category.
    confidence: float | None = Field(default=None, ge=0, le=1)


class GatherContext(ValueModel):
    """Rebuilt on every GATHERING (plan §15 decision 4); no stored model conversation."""

    original_request: dict[str, Any]
    facts: dict[str, Any]
    question: str | None = None
    reply: dict[str, str] | None = None


class Findings(ValueModel):
    category: Category
    facts: dict[str, Any] = Field(default_factory=dict)  # Keys and types per category: app/workflow/facts.py.

    @model_validator(mode="after")
    def _facts_fit_category(self) -> Self:
        facts_model(self.category).model_validate(self.facts)
        return self
    # A ticket handles one operation. Other operations in the same message are named here and not handled.
    other_requests: tuple[str, ...] = ()
    # The customer takes the request back ("forget it"): close as no_change, no rules, no writes.
    withdrawn: bool = False


class InformationCheck(ValueModel):
    missing: tuple[str, ...] = ()
    question: str = ""  # One question listing every missing item.
    close: Outcome | None = None  # Early rule rejection, e.g. an excluded service: never ask.
    reason: str = ""


class Action(StrEnum):
    ANSWER = "answer"
    NO_CHANGE = "no_change"
    CLOSE = "close"
    EXECUTE = "execute"
    ASK_AUTHORIZATION = "ask_authorization"


class AuthorizationPlan(ValueModel):
    summary: str = Field(min_length=1)
    outcome: Outcome  # Outcome after the approved calls succeed.
    on_approve: tuple[Call, ...]  # May reference results of Decision.calls.
    on_reject: tuple[Call, ...] = ()  # A "reason" arg is filled from the insurer's decision.


class Decision(ValueModel):
    action: Action
    outcome: Outcome | None = None
    reason: str = ""
    message: str = ""
    # EXECUTE: writes the model is asked to make (guard-checked). ASK_AUTHORIZATION: harness-built writes
    # that must exist before the insurer decides (claims > 1500: create_claim + pending record).
    calls: tuple[Call, ...] = ()
    authorization: AuthorizationPlan | None = None

    @model_validator(mode="after")
    def _plan_only_when_asking(self) -> Self:
        if (self.action == Action.ASK_AUTHORIZATION) != (self.authorization is not None):
            raise ValueError("authorization is required for, and only allowed with, ask_authorization.")
        return self


@runtime_checkable
class Router(Protocol):
    async def route(self, request: dict) -> RouteResult: ...  # The initial request event only.


@runtime_checkable
class AgentWorker(Protocol):
    async def gather(self, context: GatherContext, tools) -> Findings: ...
    async def propose_write(self, workflow, expected: Call, error: dict | None, tools) -> Call: ...
    async def compose_message(self, workflow, results: list[dict]) -> str: ...


@runtime_checkable
class Rules(Protocol):
    def check_information(self, workflow) -> InformationCheck: ...
    async def decide(self, workflow, reader) -> Decision: ...
