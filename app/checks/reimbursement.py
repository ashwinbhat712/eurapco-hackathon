"""Reimbursement formula (docs/policy.md). Pure: amounts in, amounts out."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.checks.money import ZERO, format_money, round_cents

REIMBURSED_SHARE = Decimal("0.80")


class Reimbursement(BaseModel):
    model_config = ConfigDict(frozen=True)

    expense: Decimal
    deductible: Decimal
    eligible_base: Decimal
    theoretical: Decimal
    remaining_limit: Decimal
    final: Decimal
    capped_by_remaining_limit: bool
    authorization_threshold: Decimal
    needs_authorization: bool  # FINAL strictly above the threshold; equality is autonomous.
    zero: bool

    def as_tool_data(self):
        return {name: format_money(value) if isinstance(value, Decimal) else value
                for name, value in self.model_dump().items()}


def calculate(expense, deductible, remaining_limit, threshold):
    """base=max(0, expense-deductible); theoretical=80% half-up to cents; final=min(theoretical, remaining)."""
    if min(expense, deductible, remaining_limit, threshold) < ZERO:
        raise ValueError("Amounts must not be negative.")
    base = max(ZERO, expense - deductible)
    theoretical = round_cents(base * REIMBURSED_SHARE)
    final = min(theoretical, remaining_limit)
    return Reimbursement(expense=expense, deductible=deductible, eligible_base=base, theoretical=theoretical,
                         remaining_limit=remaining_limit, final=final, capped_by_remaining_limit=final < theoretical,
                         authorization_threshold=threshold, needs_authorization=final > threshold,
                         zero=final == ZERO)
