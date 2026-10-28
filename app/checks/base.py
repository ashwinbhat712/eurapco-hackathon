"""Shared result shape and small parsers for the checks."""

import unicodedata
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.workflow.states import Outcome


class PolicyStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    TERMINATED = "terminated"


class Issue(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    reason: str


class CheckResult(BaseModel):
    """`issues` in rule order; the first one decides the outcome. Empty means the check passed."""

    model_config = ConfigDict(frozen=True)

    issues: tuple[Issue, ...] = ()

    @property
    def ok(self):
        return not self.issues

    @property
    def outcome(self):
        return self.issues[0].outcome if self.issues else None

    @property
    def reason(self):
        return self.issues[0].reason if self.issues else ""

    def as_tool_data(self):
        return {"ok": self.ok, "outcome": self.outcome, "reason": self.reason,
                "issues": [issue.model_dump(mode="json") for issue in self.issues]}


def collect(checks):
    """(failed, outcome, reason) triples -> CheckResult with the failed ones, order kept."""
    return CheckResult(issues=tuple(Issue(outcome=outcome, reason=reason) for failed, outcome, reason in checks
                                    if failed))


def parse_date(value):
    """ISO YYYY-MM-DD only; anything else is None (no guessing between day-first and month-first)."""
    text = value.strip() if isinstance(value, str) else ""
    try:
        return date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        return None


def same_name(left, right):
    """Exact identity after Unicode, case and whitespace normalisation; never fuzzy."""
    return bool(left and right) and _name_key(left) == _name_key(right)


def _name_key(name):
    return " ".join(unicodedata.normalize("NFKC", str(name)).casefold().split())


def same_identifier(left, right):
    return bool(left and right) and str(left).strip() == str(right).strip()
