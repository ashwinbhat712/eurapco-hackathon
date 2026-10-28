"""Local (non-MCP) tools: the deterministic checks, callable by an agent instead of doing the maths itself.

Each tool reads current facts through the agent's own DomainClient (so the reads are logged and scoped),
binds the ticket's policy_id itself (a model can never check another policy), and returns the shared
`{"ok": ..., "data" | "error": ...}` envelope. Nothing here writes.
"""

import json

from app.checks.coverage import check_coverage_change
from app.checks.duplicates import find_duplicate
from app.checks.eligibility import check_eligibility
from app.checks.money import parse_money
from app.checks.reimbursement import calculate

_STRING = {"type": "string"}
_EXPENSE = {"type": "string", "description": "Invoice total in EUR as on the invoice, e.g. 1925.00."}
_DATE = {"type": "string", "description": "YYYY-MM-DD."}


def _invalid(message, field):
    return {"ok": False, "error": {"code": "INVALID_ARGUMENT", "message": message, "retryable": True,
                                   "effect_applied": False, "field": field}}


class LocalTools:
    def __init__(self, domain, policy_id):
        self.domain, self.policy_id = domain, policy_id

    async def _read(self, tool):
        return await self.domain.call(tool, {"policy_id": self.policy_id})

    async def calculate_reimbursement(self, expense):
        amount = parse_money(expense)
        if amount is None:
            return _invalid("expense must be a non-negative EUR amount with at most two decimals.", "expense")
        read = await self._read("get_policy")
        if not read["ok"]:
            return read
        terms, thresholds = read["data"]["financial_terms"], read["data"]["authorization_thresholds"]
        result = calculate(amount, parse_money(terms["deductible"]), parse_money(terms["remaining_limit"]),
                           parse_money(thresholds["reimbursement"]))
        return {"ok": True, "data": result.as_tool_data()}

    async def check_reimbursement_eligibility(self, patient_name, service_category, service_date, invoice_date,
                                              expense, currency="EUR"):
        read = await self._read("get_policy")
        if not read["ok"]:
            return read
        result = check_eligibility(read["data"], patient_name=patient_name, service_category=service_category,
                                   service_date=service_date, invoice_date=invoice_date, expense=expense,
                                   currency=currency)
        return {"ok": True, "data": result.as_tool_data()}

    async def find_duplicate_claim(self, provider_id, invoice_number, invoice_date):
        read = await self._read("list_claims")
        if not read["ok"]:
            return read
        result = find_duplicate(read["data"]["claims"], provider_id=provider_id, invoice_number=invoice_number,
                                invoice_date=invoice_date)
        return {"ok": True, "data": result.as_tool_data()}

    async def check_coverage_change(self, annual_limit=None, deductible=None):
        read = await self._read("get_policy")
        if not read["ok"]:
            return read
        result = check_coverage_change(read["data"], annual_limit=annual_limit, deductible=deductible)
        return {"ok": True, "data": result.as_tool_data()}


SPECS = {
    "calculate_reimbursement": (
        "Exact reimbursement for an invoice amount on this policy: deductible, 80%, half-up cents, capped by the "
        "remaining annual limit, and whether insurer authorization is needed. Use it instead of computing.",
        {"expense": _EXPENSE}, ["expense"]),
    "check_reimbursement_eligibility": (
        "Checks policy status, covered/excluded service, insured patient, coverage period, invoice vs service "
        "date, currency and amount. Returns every issue; the first decides the outcome.",
        {"patient_name": _STRING, "service_category": _STRING, "service_date": _DATE, "invoice_date": _DATE,
         "expense": _EXPENSE, "currency": _STRING},
        ["patient_name", "service_category", "service_date", "invoice_date", "expense"]),
    "find_duplicate_claim": (
        "Looks for an existing record with the same provider ID, invoice number and invoice-date year. "
        "If duplicate is true, report existing_claim_id; never register or pay again.",
        {"provider_id": _STRING, "invoice_number": _STRING, "invoice_date": _DATE},
        ["provider_id", "invoice_number", "invoice_date"]),
    "check_coverage_change": (
        "Checks a requested annual limit and/or deductible: catalog values, increases only, atomic, and "
        "whether insurer approval is needed. Returns only the fields that really change.",
        {"annual_limit": _EXPENSE | {"description": "Requested final annual limit, e.g. 5000.00."},
         "deductible": _EXPENSE | {"description": "Requested final deductible, e.g. 200.00."}}, []),
}
LOCAL_ACCESS = {
    "administration": ("check_coverage_change",),
    "claims": ("calculate_reimbursement", "check_reimbursement_eligibility", "find_duplicate_claim"),
}
LOCAL_TOOL_GUIDANCE = {
    "administration": "Before any update_coverage call, call check_coverage_change with the requested final values "
                      "and follow it: issues mean close without writing, no_change means nothing to write, "
                      "needs_authorization means ask the insurer with exactly the returned changes.",
    "claims": "For a new reimbursement call check_reimbursement_eligibility, find_duplicate_claim and "
              "calculate_reimbursement before create_claim or pay_claim, and follow their results. Never compute "
              "amounts yourself: pay and propose exactly the returned final amount; needs_authorization decides "
              "whether insurer approval is required.",
}


def datapizza_tools(agent, domain, policy_id, log=None):
    """datapizza Tools for one agent. Calls and results are logged like MCP calls, marked local."""
    from datapizza.tools import Tool

    local = LocalTools(domain, policy_id)

    def make(name):
        description, properties, required = SPECS[name]

        async def invoke(**arguments):
            known = {key: value for key, value in arguments.items() if key in properties}
            if log:
                log.event("tool_call", name=name, arguments=known, local=True)
            missing = [key for key in required if known.get(key) in (None, "")]
            result = (_invalid(f"{missing[0]} is required.", missing[0]) if missing
                      else await getattr(local, name)(**known))
            if log:
                log.event("tool_result", name=name, result=result, local=True)
            return json.dumps(result, ensure_ascii=False)

        return Tool(func=invoke, name=name, description=description, properties=properties, required=required)

    return [make(name) for name in LOCAL_ACCESS[agent]]
