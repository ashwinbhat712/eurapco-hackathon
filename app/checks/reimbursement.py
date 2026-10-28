"""Reimbursement formula (docs/policy.md). Pure: amounts in, amounts out."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.checks.money import Money

REIMBURSED_SHARE = Decimal("0.80")
PERCENT_FORMAT = ".0%"


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

    @classmethod
    def calculate(cls, expense, deductible, remaining_limit, threshold):
        """base=max(0, expense-deductible); theoretical=80% half-up to cents; final=min(theoretical, remaining)."""
        amounts = (expense, deductible, remaining_limit, threshold)
        if any(amount is None for amount in amounts) or min(amounts) < Money.ZERO:
            raise ValueError("Amounts must be present and not negative.")
        base = max(Money.ZERO, expense - deductible)
        theoretical = Money.round(base * REIMBURSED_SHARE)
        final = min(theoretical, remaining_limit)
        return cls(expense=expense, deductible=deductible, eligible_base=base, theoretical=theoretical,
                   remaining_limit=remaining_limit, final=final, capped_by_remaining_limit=final < theoretical,
                   authorization_threshold=threshold, needs_authorization=final > threshold,
                   zero=final == Money.ZERO)

    @classmethod
    def for_policy(cls, policy, expense):
        """The policy's current deductible, remaining limit and threshold applied to `expense`."""
        terms, thresholds = policy["financial_terms"], policy["authorization_thresholds"]
        return cls.calculate(Money.parse(expense), Money.parse(terms["deductible"]),
                             Money.parse(terms["remaining_limit"]), Money.parse(thresholds["reimbursement"]))

    def as_tool_data(self):
        return {name: Money.format(value) if isinstance(value, Decimal) else value
                for name, value in self.model_dump().items()}

    def explain(self):
        """The docs/policy.md formula with this ticket's figures, so the customer can follow the amount."""
        money = Money.with_currency
        steps = [f"invoice {money(self.expense)} minus deductible {money(self.deductible)} "
                 f"= {money(self.eligible_base)}",
                 f"{format(REIMBURSED_SHARE, PERCENT_FORMAT)} of that = {money(self.theoretical)}"]
        steps += ([f"capped at the remaining annual limit {money(self.remaining_limit)}"]
                  if self.capped_by_remaining_limit else [])
        return "; ".join(steps) + f"; reimbursement {money(self.final)}."
