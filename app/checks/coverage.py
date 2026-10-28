"""Coverage change (docs/policy.md): catalog values, increases only, atomic, insurer approval above thresholds."""

from pydantic import BaseModel, ConfigDict

from app.checks.base import CheckResult, PolicyStatus, collect
from app.checks.money import format_money, parse_money
from app.workflow.states import Outcome

CATALOG_KEYS = {"annual_limit": "annual_limits", "deductible": "deductibles"}  # Field -> allowed_values key.


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


def _field_issues(field, raw, policy):
    value = parse_money(raw)
    current = parse_money(policy["financial_terms"][field])
    allowed = [parse_money(item) for item in policy["allowed_values"][CATALOG_KEYS[field]]]
    listing = ", ".join(policy["allowed_values"][CATALOG_KEYS[field]])
    return (
        (value is None, Outcome.INVALID_INPUT, f"{field} {raw!r} is not a valid EUR amount."),
        (value is not None and value not in allowed, Outcome.INVALID_INPUT,
         f"{field} {raw} is not offered; allowed values are {listing}."),
        (value is not None and value in allowed and value < current, Outcome.INVALID_INPUT,
         f"{field} can only be increased (current {format_money(current)})."),
    )


def check_coverage_change(policy, *, annual_limit=None, deductible=None):
    """Atomic: one inadmissible field rejects the whole change. Authorization is judged on changed fields only."""
    requested = {field: raw for field, raw in (("annual_limit", annual_limit), ("deductible", deductible))
                 if raw not in (None, "")}
    result = collect((
        (policy["status"] != PolicyStatus.ACTIVE, Outcome.INACTIVE_POLICY, f"The policy is {policy['status']}."),
        (not requested, Outcome.INVALID_INPUT, "No new annual limit or deductible was given."),
        *(issue for field, raw in requested.items() for issue in _field_issues(field, raw, policy)),
    ))
    if not result.ok:
        return CoverageCheck(result=result)
    terms, thresholds = policy["financial_terms"], policy["authorization_thresholds"]
    changes = {field: format_money(parse_money(raw)) for field, raw in requested.items()
               if parse_money(raw) != parse_money(terms[field])}
    needs = any(parse_money(value) > parse_money(thresholds[field]) for field, value in changes.items())
    return CoverageCheck(result=result, changes=changes, needs_authorization=needs)
