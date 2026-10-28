"""Coverage change (docs/policy.md): catalog values, increases only, atomic, insurer approval above thresholds."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.checks.base import CheckResult, PolicyStatus
from app.checks.money import Money
from app.workflow.states import Outcome


class CoverageField(StrEnum):
    ANNUAL_LIMIT = "annual_limit"
    DEDUCTIBLE = "deductible"


class CoverageCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    result: CheckResult
    changes: dict[str, str] = {}  # Only fields whose value really changes, in the tools' Money format.
    needs_authorization: bool = False

    @property
    def no_change(self):
        return self.result.ok and not self.changes

    def as_tool_data(self):
        return {**self.result.as_tool_data(), "changes": self.changes, "no_change": self.no_change,
                "needs_authorization": self.needs_authorization}


class CoverageChangeCheck:
    """Atomic: one inadmissible field rejects the whole change. Authorization is judged on changed fields only."""

    CATALOG_KEYS = {CoverageField.ANNUAL_LIMIT: "annual_limits", CoverageField.DEDUCTIBLE: "deductibles"}

    def __init__(self, policy):
        self.policy = policy

    def check(self, *, annual_limit=None, deductible=None):
        policy = self.policy
        given = {CoverageField.ANNUAL_LIMIT: annual_limit, CoverageField.DEDUCTIBLE: deductible}
        requested = {field: raw for field, raw in given.items() if raw not in (None, "")}
        result = CheckResult.collect((
            (policy["status"] != PolicyStatus.ACTIVE, Outcome.INACTIVE_POLICY, f"The policy is {policy['status']}."),
            (not requested, Outcome.INVALID_INPUT, "No new annual limit or deductible was given."),
            *(issue for field, raw in requested.items() for issue in self._field_issues(field, raw)),
        ))
        if not result.ok:
            return CoverageCheck(result=result)
        terms, thresholds = policy["financial_terms"], policy["authorization_thresholds"]
        changes = {str(field): Money.format(Money.parse(raw)) for field, raw in requested.items()
                   if Money.parse(raw) != Money.parse(terms[field])}
        needs = any(Money.parse(value) > Money.parse(thresholds[field]) for field, value in changes.items())
        return CoverageCheck(result=result, changes=changes, needs_authorization=needs)

    def _field_issues(self, field, raw):
        value = Money.parse(raw)
        current = Money.parse(self.policy["financial_terms"][field])
        catalog = self.policy["allowed_values"][self.CATALOG_KEYS[field]]
        allowed = [Money.parse(item) for item in catalog]
        return (
            (value is None, Outcome.INVALID_INPUT, f"{field} {raw!r} is not a valid EUR amount."),
            (value is not None and value not in allowed, Outcome.INVALID_INPUT,
             f"{field} {raw} is not offered; allowed values are {', '.join(catalog)}."),
            (value is not None and value in allowed and value < current, Outcome.INVALID_INPUT,
             f"{field} can only be increased (current {Money.format(current)})."),
        )
