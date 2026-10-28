"""Local (non-MCP) tools: the deterministic checks, callable by an agent instead of doing the maths itself.

Each tool reads current facts through the agent's own DomainClient (so the reads are logged and scoped),
binds the ticket's policy_id itself (a model can never check another policy), and returns the shared
`{"ok": ..., "data" | "error": ...}` envelope. Nothing here writes.
"""

from abc import ABC, abstractmethod
from typing import ClassVar

from app.checks.base import IsoDate
from app.checks.coverage import CoverageChangeCheck, CoverageField
from app.checks.duplicates import DuplicateFinder
from app.checks.eligibility import EligibilityCheck
from app.checks.money import Money
from app.checks.reimbursement import Reimbursement
from app.contracts import Tool

_STRING = {"type": "string"}
_EXPENSE = {"type": "string", "description": "Invoice total in EUR as on the invoice, e.g. 1925.00."}
_DATE = {"type": "string", "description": "YYYY-MM-DD."}


class Envelope:
    """The shared `{"ok": ..., "data" | "error": ...}` result shape (contracts/tools.schema.json ToolError)."""

    @staticmethod
    def ok(data):
        return {"ok": True, "data": data}

    @staticmethod
    def invalid(message, field):
        """The model sent something unusable: retryable with a corrected argument."""
        return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": message, "retryable": True,
                                       "effect_applied": False, "field": field}}

    @staticmethod
    def failed(message):
        """The current domain data could not be evaluated: retrying the same call will not help."""
        return {"ok": False, "error": {"code": "OPERATION_FAILED", "message": message, "retryable": False,
                                       "effect_applied": False}}


class LocalTool(ABC):
    """One read-only check. Reads fresh data for the bound policy, validates, evaluates, never raises."""

    name: ClassVar[str]
    description: ClassVar[str]
    properties: ClassVar[dict]
    required: ClassVar[tuple[str, ...]] = ()
    reads: ClassVar[Tool] = Tool.GET_POLICY
    # Malformed domain data (missing key, unreadable amount or date) must become an error envelope, not crash
    # the agent loop in the middle of a ticket.
    _DATA_ERRORS = (KeyError, TypeError, ValueError, AttributeError)

    def __init__(self, reader, policy_id):
        """reader: a ReadCache (or anything with `async read(name, arguments)`)."""
        self.reader, self.policy_id = reader, policy_id

    async def __call__(self, **arguments):
        missing = [key for key in self.required if arguments.get(key) in (None, "")]
        if missing:
            return Envelope.invalid(f"Missing required argument(s): {', '.join(missing)}.", missing[0])
        invalid = self.validate(**arguments)
        if invalid:
            return invalid
        read = await self.reader.read(self.reads, {"policy_id": self.policy_id})
        if not read.get("ok"):
            return read
        try:
            return Envelope.ok(self.evaluate(read["data"], **arguments))
        except self._DATA_ERRORS as exc:
            return Envelope.failed(f"{self.name} could not evaluate the current {self.reads} data ({exc!r}).")

    def validate(self, **arguments):
        """An INVALID_ARGUMENT envelope for arguments that cannot be checked at all; None if usable."""
        return None

    @abstractmethod
    def evaluate(self, data, **arguments):
        """Tool data from the fresh read and the arguments."""


class CalculateReimbursement(LocalTool):
    name = "calculate_reimbursement"
    description = ("Exact reimbursement for an invoice amount on this policy: deductible, 80%, half-up cents, capped "
                   "by the remaining annual limit, and whether insurer authorization is needed. Use it instead of "
                   "computing.")
    properties = {"expense": _EXPENSE}
    required = ("expense",)

    def validate(self, expense):
        return (Envelope.invalid("expense must be a non-negative EUR amount with at most two decimals.", "expense")
                if Money.parse(expense) is None else None)

    def evaluate(self, policy, expense):
        return Reimbursement.for_policy(policy, expense).as_tool_data()


class CheckReimbursementEligibility(LocalTool):
    name = "check_reimbursement_eligibility"
    description = ("Checks policy status, covered/excluded service, insured patient, coverage period, invoice vs "
                   "service date, currency and amount. Returns every issue; the first decides the outcome.")
    properties = {"patient_name": _STRING, "service_category": _STRING, "service_date": _DATE,
                  "invoice_date": _DATE, "expense": _EXPENSE, "currency": _STRING}
    required = ("patient_name", "service_category", "service_date", "invoice_date", "expense")

    def evaluate(self, policy, **facts):
        return EligibilityCheck(policy).check(**facts).as_tool_data()


class FindDuplicateClaim(LocalTool):
    name = "find_duplicate_claim"
    description = ("Looks for an existing record with the same provider ID, invoice number and invoice-date year. "
                   "If duplicate is true, report existing_claim_id; never register or pay again.")
    properties = {"provider_id": _STRING, "invoice_number": _STRING, "invoice_date": _DATE}
    required = ("provider_id", "invoice_number", "invoice_date")
    reads = Tool.LIST_CLAIMS

    def validate(self, invoice_date, **_):
        # Without a readable year the key matches nothing, which would wrongly read as "no duplicate".
        return (Envelope.invalid("invoice_date must be YYYY-MM-DD.", "invoice_date")
                if IsoDate.parse(invoice_date) is None else None)

    def evaluate(self, listing, **key):
        return DuplicateFinder(listing["claims"]).find(**key).as_tool_data()


class CheckCoverageChange(LocalTool):
    name = "check_coverage_change"
    description = ("Checks a requested annual limit and/or deductible: catalog values, increases only, atomic, and "
                   "whether insurer approval is needed. Returns only the fields that really change.")
    properties = {CoverageField.ANNUAL_LIMIT: _EXPENSE | {"description": "Requested final annual limit, e.g. 5000.00."},
                  CoverageField.DEDUCTIBLE: _EXPENSE | {"description": "Requested final deductible, e.g. 200.00."}}

    def validate(self, **values):
        return (None if any(value not in (None, "") for value in values.values())
                else Envelope.invalid("Give annual_limit, deductible or both.", CoverageField.ANNUAL_LIMIT))

    def evaluate(self, policy, **values):
        return CoverageChangeCheck(policy).check(**values).as_tool_data()
