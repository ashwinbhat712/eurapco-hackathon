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


class ClaimStatus(StrEnum):
    """contracts/tools.schema.json ClaimStatus."""

    REGISTERED = "registered"
    PENDING_AUTHORIZATION = "pending_authorization"
    PAID = "paid"
    REJECTED = "rejected"
    NOT_COVERED = "not_covered"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"
    PAYMENT_FAILED = "payment_failed"


class Service(StrEnum):
    """contracts/tools.schema.json Service; covered and excluded sets are in docs/policy.md."""

    SPECIALIST_CONSULTATION = "specialist_consultation"
    DIAGNOSTIC_TEST = "diagnostic_test"
    PHYSIOTHERAPY = "physiotherapy"
    DENTAL_CARE = "dental_care"
    COSMETIC_TREATMENT = "cosmetic_treatment"
    MEDICINES = "medicines"


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

    @classmethod
    def collect(cls, checks):
        """(failed, outcome, reason) triples -> a result with the failed ones, order kept."""
        return cls(issues=tuple(Issue(outcome=outcome, reason=reason) for failed, outcome, reason in checks
                                if failed))


class IsoDate:
    @staticmethod
    def parse(value):
        """ISO YYYY-MM-DD only; anything else is None (no guessing between day-first and month-first)."""
        text = value.strip() if isinstance(value, str) else ""
        try:
            return date.fromisoformat(text) if len(text) == 10 else None
        except ValueError:
            return None

    @classmethod
    def year(cls, value):
        parsed = cls.parse(value)
        return parsed.year if parsed else None


class Same:
    """Exact identity checks; never fuzzy."""

    @classmethod
    def name(cls, left, right):
        """Equal after Unicode, case and whitespace normalisation."""
        return bool(left and right) and cls._name_key(left) == cls._name_key(right)

    @staticmethod
    def identifier(left, right):
        """Equal after trimming; case matters."""
        return bool(left and right) and str(left).strip() == str(right).strip()

    @staticmethod
    def _name_key(name):
        return " ".join(unicodedata.normalize("NFKC", str(name)).casefold().split())
