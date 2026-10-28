"""Duplicate detection (docs/policy.md): provider ID + invoice number + invoice-date year. Exact, never fuzzy."""

from pydantic import BaseModel, ConfigDict

from app.checks.base import parse_date, same_identifier

# Records that already stand for this invoice: paid, waiting for the insurer, or registered/failed and still
# open for payment. Re-registering any of them would create a second record or a second payment.
BLOCKING_STATUSES = frozenset({"paid", "pending_authorization", "registered", "payment_failed"})


class DuplicateCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    duplicate: dict | None = None  # The blocking existing record, if any.
    closed_matches: tuple[str, ...] = ()  # Same invoice, closed without payment (rejected, invalid, ...).
    invalid_key: bool = False  # The new invoice's own key could not be read.

    @property
    def found(self):
        return self.duplicate is not None

    def as_tool_data(self):
        existing = self.duplicate or {}
        return {"duplicate": self.found, "invalid_key": self.invalid_key, "existing_claim_id": existing.get("claim_id"),
                "existing_status": existing.get("status"), "existing_payment": existing.get("payment"),
                "closed_matches": list(self.closed_matches)}


def _year(value):
    parsed = parse_date(value)
    return parsed.year if parsed else None


def _matches(claim, provider_id, invoice_number, year):
    invoice = claim.get("invoice") or {}
    return (same_identifier(invoice.get("provider_id"), provider_id)
            and same_identifier(invoice.get("invoice_number"), invoice_number)
            and _year(invoice.get("invoice_date")) == year)


def find_duplicate(claims, *, provider_id, invoice_number, invoice_date):
    year = _year(invoice_date)
    if year is None or not provider_id or not invoice_number:
        return DuplicateCheck(invalid_key=True)
    matches = [claim for claim in claims if _matches(claim, provider_id, invoice_number, year)]
    blocking = [claim for claim in matches if claim.get("status") in BLOCKING_STATUSES]
    paid_first = sorted(blocking, key=lambda claim: claim.get("status") != "paid")
    return DuplicateCheck(duplicate=paid_first[0] if paid_first else None,
                          closed_matches=tuple(claim["claim_id"] for claim in matches if claim not in blocking))
