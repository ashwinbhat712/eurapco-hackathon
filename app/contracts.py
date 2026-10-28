"""Shared payload validation. Schemas describe contracts, not business gates."""

import copy
import json
import time
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


class Tool(StrEnum):
    GET_POLICY = "get_policy"
    UPDATE_CONTACT_DETAILS = "update_contact_details"
    UPDATE_COVERAGE = "update_coverage"
    TERMINATE_POLICY = "terminate_policy"
    GET_CLAIM = "get_claim"
    LIST_CLAIMS = "list_claims"
    CREATE_CLAIM = "create_claim"
    PAY_CLAIM = "pay_claim"
    RECORD_CLAIM_OUTCOME = "record_claim_outcome"


TOOLS = {
    Tool.GET_POLICY: ("GetPolicy", "Read the current policy, contacts, insured people and financial terms."),
    Tool.UPDATE_CONTACT_DETAILS: ("UpdateContactDetails", "Update only requested email, phone or full postal address."),
    Tool.UPDATE_COVERAGE: ("UpdateCoverage", "Update annual limit and/or deductible atomically, using supported catalog values."),
    Tool.TERMINATE_POLICY: ("TerminatePolicy", "Simulate termination of the referenced policy."),
    Tool.GET_CLAIM: ("GetClaim", "Read one existing medical reimbursement record; do not reopen it."),
    Tool.LIST_CLAIMS: ("ListClaims", "Read the policy's complete initial and current-run reimbursement history."),
    Tool.CREATE_CLAIM: ("CreateClaim", "Create a medical reimbursement record from actual invoice facts and evidence references."),
    Tool.PAY_CLAIM: ("PayClaim", "Simulate a payment and return the actual result; never a real transfer."),
    Tool.RECORD_CLAIM_OUTCOME: ("RecordClaimOutcome", "Store an actual outcome on an existing reimbursement record; does not pay."),
}
ACCESS = {
    "administration": (Tool.GET_POLICY, Tool.UPDATE_CONTACT_DETAILS, Tool.UPDATE_COVERAGE, Tool.TERMINATE_POLICY),
    "claims": (Tool.GET_POLICY, Tool.GET_CLAIM, Tool.LIST_CLAIMS, Tool.CREATE_CLAIM, Tool.PAY_CLAIM,
               Tool.RECORD_CLAIM_OUTCOME),
}


@lru_cache
def document(filename):
    return json.loads((ROOT / "contracts" / filename).read_text(encoding="utf-8"))


def validator(filename, definition=None):
    schema = document(filename)
    if definition:
        schema = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
    return Draft202012Validator(schema, format_checker=FormatChecker())


def tool_schema(name, *, model_facing=False):
    """Expand local refs for provider APIs; bind run_id in the client, not model."""
    definitions = document("tools.schema.json")["$defs"]

    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {k: expand(v) for k, v in value.items()}
        return [expand(v) for v in value] if isinstance(value, list) else copy.deepcopy(value)

    schema = expand(definitions[TOOLS[name][0] + "Input"])
    if model_facing:
        schema["properties"].pop("run_id")
        schema["required"].remove("run_id")
    return schema


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Windows denies the rename while a sync client/antivirus briefly holds the target open.
    for delay in (0.05, 0.1, 0.2, 0.4, 0.8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(delay)
    temporary.replace(path)
