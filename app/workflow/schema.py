"""Shared pydantic bases and field types. Persisted state and port values are validated, never trusted."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.contracts import document

SAFE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,100}$"  # Ticket IDs name files; never let one escape the directory.


class Vocabulary:
    """Request categories from contracts/vocabulary.json; anything else is refused."""

    BY_AGENT = {agent: frozenset(categories)
                for agent, categories in document("vocabulary.json")["request_categories"].items()}
    CATEGORIES = frozenset(category for categories in BY_AGENT.values() for category in categories)

    @classmethod
    def category(cls, value):
        if value not in cls.CATEGORIES:
            raise ValueError(f"{value!r} is not a request category in contracts/vocabulary.json.")
        return value

    @classmethod
    def owner(cls, category):
        """The agent whose list holds a category that only one agent has."""
        return next(agent for agent, categories in cls.BY_AGENT.items() if category in categories)


SafeId = Annotated[str, Field(pattern=SAFE_ID_PATTERN)]
Category = Annotated[str, AfterValidator(Vocabulary.category)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ValueModel(BaseModel):
    """An immutable value: port contracts (router, agents, rules) and in-process results. Unknown fields refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RuntimeModel(BaseModel):
    """In-process wiring that holds live objects (stores, clients, fakes). Checked by isinstance, never persisted."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, validate_assignment=True)


class StateModel(BaseModel):
    """Persisted, mutable state. Every assignment is validated, so a bad value fails where it is written."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
