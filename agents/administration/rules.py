"""Deterministic administration rules (challenge Part 4).

Pure decision functions: no I/O, no model calls, no tool calls. They take the
extracted intent, a fresh `get_policy` payload and the workflow state, and return a
single `Decision`. The executor in `agent.py` performs the effect or the suspension.

Documented team choices
-----------------------
* A coverage change requesting a value **lower** than the current one closes as
  `coverage_change` / `closed_without_execution` / `invalid_input`; the category stays
  the requested operation instead of becoming an out-of-scope one.
* A termination message **without** an explicit instruction to terminate is handled as
  `policy_enquiry` / `completed` / `answered`; the single clarification is not spent on
  asking whether the policyholder really means it.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

ADDRESS_FIELDS = ("street", "city", "postal_code", "country")
CONTACT_FIELDS = ("email", "phone", "address")
COVERAGE_FIELDS = ("annual_limit", "deductible")
ADMIN_CATEGORIES = ("policy_enquiry", "contact_update", "coverage_change", "policy_termination",
                    "out_of_scope_iban", "out_of_scope_renewal", "out_of_scope_claims",
                    "out_of_scope_other", "unclear")
OUT_OF_SCOPE_CATEGORIES = ("out_of_scope_iban", "out_of_scope_renewal", "out_of_scope_claims", "out_of_scope_other")

PHONE_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

FIELD_LABELS = {"email": "the new email address",
                "phone": "the new telephone number in international format (for example +393339998888)",
                "address": "the full postal address",
                "annual_limit": "the intended final annual limit (3000, 5000 or 10000 EUR)",
                "deductible": "the intended final per-claim deductible (50, 100, 200 or 500 EUR)"}
ADDRESS_LABELS = {"street": "the street", "city": "the city",
                  "postal_code": "the postal code", "country": "the country"}


@dataclass
class Decision:
    """What the administration harness must do next. Exactly one action at a time."""

    request_category: str
    agent_state: str
    request_outcome: str
    reason: str
    message: str
    question: Optional[str] = None
    proposal: Optional[dict] = None
    effect: Optional[dict] = None
    reroute: Optional[str] = None
    answer_from_policy: bool = False
    notes: list = field(default_factory=list)


def money(value):
    return Decimal(str(value))


def _status(policy):
    return policy.get("status")


def _thresholds(policy):
    return policy.get("authorization_thresholds", {})


def _catalog(policy):
    allowed = policy.get("allowed_values", {})
    return {"annual_limit": tuple(allowed.get("annual_limits", ())),
            "deductible": tuple(allowed.get("deductibles", ()))}


def _inactive(category, policy):
    return Decision(category, "closed_without_execution", "inactive_policy",
                    f"Policy {policy.get('policy_id')} is {_status(policy)}, so the operation cannot be performed.",
                    f"Your policy is currently {_status(policy)}. This change can only be made on an active policy, "
                    "so nothing has been modified.")


def _one_question(category, items, policy_id):
    listed = "; ".join(items)
    return Decision(category, "awaiting_clarification", "information_requested",
                    f"Required information is missing for {category} on {policy_id}: {listed}.",
                    "Before this request can be processed, some information is still needed.",
                    question="To complete your request, please send all of the following in one reply: " + listed + ".")


def _insufficient(category, items, policy_id):
    listed = "; ".join(items)
    return Decision(category, "closed_without_execution", "insufficient_information",
                    f"Still missing after the single clarification on {policy_id}: {listed}.",
                    "The reply did not contain all the information required, so the request has been closed without "
                    "any change. Still missing: " + listed + ". Please send a new request with the complete details.")


def summarize_policy(policy, *, termination_context=False):
    """Factual, deterministic answer built only from the retrieved policy."""
    terms = policy.get("financial_terms", {})
    period = policy.get("coverage_period", {})
    contacts = policy.get("contacts", {})
    address = contacts.get("address", {})
    lines = [f"Policy {policy.get('policy_id')} is {_status(policy)}.",
             "Insured people: " + ", ".join(person["name"] for person in policy.get("insured_people", [])) + ".",
             f"Coverage period: {period.get('start')} to {period.get('end')} (both days included).",
             "Covered services: " + ", ".join(policy.get("covered_services", [])) + ".",
             "Excluded services: " + ", ".join(policy.get("excluded_services", [])) + ".",
             f"Annual limit: EUR {terms.get('annual_limit')}; per-claim deductible: EUR {terms.get('deductible')}; "
             f"remaining limit: EUR {terms.get('remaining_limit')}.",
             f"Contacts on file: {contacts.get('email')}, {contacts.get('phone')}, "
             f"{address.get('street')}, {address.get('postal_code')} {address.get('city')}, {address.get('country')}."]
    if termination_context:
        lines.append("To terminate the policy we need an explicit instruction from you, and the termination is "
                     "always submitted to the insurer for authorization before it takes effect. A personal reason "
                     "is not required.")
    return " ".join(lines)


def decide(intent, policy, workflow):
    """Entry point for an initial request or a clarification reply."""
    category = intent.request_category
    if category == "contact_update":
        return decide_contact_update(intent, policy, workflow)
    if category == "coverage_change":
        return decide_coverage_change(intent, policy, workflow)
    if category == "policy_termination":
        return decide_termination(intent, policy, workflow)
    if category == "policy_enquiry":
        return decide_enquiry(intent, policy)
    if category in OUT_OF_SCOPE_CATEGORIES:
        return decide_out_of_scope(category, policy)
    return decide_unclear(policy)


def decide_enquiry(intent, policy, *, termination_context=False):
    return Decision("policy_enquiry", "completed", "answered",
                    f"Policy enquiry answered from the current record of {policy.get('policy_id')}.",
                    summarize_policy(policy, termination_context=termination_context),
                    answer_from_policy=True)


def decide_out_of_scope(category, policy):
    explanations = {
        "out_of_scope_iban": "Changing the bank account or IBAN is not an operation this service can perform.",
        "out_of_scope_renewal": "Policy renewal is not an operation this service can perform.",
        "out_of_scope_claims": "This is a medical reimbursement request, which is handled by the claims service, "
                               "not by policy administration.",
        "out_of_scope_other": "This operation is not supported by policy administration.",
    }
    return Decision(category, "closed_without_execution", "out_of_scope",
                    f"Unsupported administration operation on {policy.get('policy_id')}: {category}.",
                    explanations[category] + " Nothing has been changed on your policy.",
                    reroute="claims" if category == "out_of_scope_claims" else None)


def decide_unclear(policy):
    return Decision("unclear", "closed_without_execution", "unclear",
                    f"No administration operation could be identified for {policy.get('policy_id')}.",
                    "We could not identify which operation you are asking for, so nothing has been changed. "
                    "Please send a new message describing the change you need.")


def decide_contact_update(intent, policy, workflow):
    if _status(policy) != "active":
        return _inactive("contact_update", policy)

    requested = list(intent.contact_fields)
    unsupported = [name for name in requested if name not in CONTACT_FIELDS]
    supported = [name for name in requested if name in CONTACT_FIELDS]
    if unsupported and not supported:
        category = "out_of_scope_iban" if "iban" in unsupported else "out_of_scope_other"
        return decide_out_of_scope(category, policy)

    missing = []
    if not supported:
        missing.append("which contact detail you want to change (email, telephone or postal address) and its new value")
    for name in supported:
        if name == "email" and not intent.email:
            missing.append(FIELD_LABELS["email"])
        if name == "phone" and not intent.phone:
            missing.append(FIELD_LABELS["phone"])
        if name == "address":
            absent = [ADDRESS_LABELS[part] for part in ADDRESS_FIELDS if not intent.address.get(part)]
            if len(absent) == len(ADDRESS_FIELDS):
                missing.append(FIELD_LABELS["address"] + " (street, city, postal code and country)")
            else:
                missing.extend(f"{label} of the new postal address" for label in absent)
    if missing:
        if workflow.clarification_asked:
            return _insufficient("contact_update", missing, policy.get("policy_id"))
        return _one_question("contact_update", missing, policy.get("policy_id"))

    invalid = []
    if "phone" in supported and not PHONE_PATTERN.fullmatch(intent.phone or ""):
        invalid.append(f"the telephone number '{intent.phone}' is not in international format (+ and 8 to 15 digits)")
    if "email" in supported and not EMAIL_PATTERN.fullmatch(intent.email or ""):
        invalid.append(f"the email address '{intent.email}' is not a valid address")
    if invalid:
        listed = "; ".join(invalid)
        return Decision("contact_update", "closed_without_execution", "invalid_input",
                        f"Rejected contact values for {policy.get('policy_id')}: {listed}.",
                        "The request could not be accepted as sent because " + listed
                        + ". No contact detail has been changed.")

    contacts = policy.get("contacts", {})
    proposed = {}
    if "email" in supported:
        proposed["email"] = intent.email
    if "phone" in supported:
        proposed["phone"] = intent.phone
    if "address" in supported:
        proposed["address"] = {part: intent.address[part] for part in ADDRESS_FIELDS}
    changes = {name: value for name, value in proposed.items() if contacts.get(name) != value}

    notes = []
    if unsupported:
        notes.append("Changing " + ", ".join(sorted(unsupported)) + " is not supported by this service"
                     + (" (IBAN changes are out of scope)." if "iban" in unsupported else "."))
    if not changes:
        return Decision("contact_update", "completed", "no_change",
                        f"All requested contact values already match the record of {policy.get('policy_id')}.",
                        "The contact details you sent are already the ones on file, so nothing needed to be changed. "
                        + " ".join(notes), notes=notes)
    return Decision("contact_update", "completed", "updated",
                    "Contact update admissible; only the changed fields are written.",
                    "Applying the requested contact update.",
                    effect={"tool": "update_contact_details",
                            "args": {"policy_id": policy["policy_id"], "changes": changes}}, notes=notes)


def decide_coverage_change(intent, policy, workflow):
    if _status(policy) != "active":
        return _inactive("coverage_change", policy)

    terms = policy.get("financial_terms", {})
    catalog = _catalog(policy)
    requested = {name: getattr(intent, name) for name in COVERAGE_FIELDS}
    asked_for = [name for name in COVERAGE_FIELDS if name in intent.coverage_fields or requested[name]]

    missing = [FIELD_LABELS[name] for name in asked_for if not requested[name]]
    if not asked_for:
        missing = [FIELD_LABELS["annual_limit"] + " and/or " + FIELD_LABELS["deductible"]]
    if missing:
        if workflow.clarification_asked:
            return _insufficient("coverage_change", missing, policy.get("policy_id"))
        return _one_question("coverage_change", missing, policy.get("policy_id"))

    supplied = {name: requested[name] for name in asked_for if requested[name]}
    rejected = []
    for name, value in supplied.items():
        if value not in catalog[name]:
            rejected.append(f"EUR {value} is not a supported {name.replace('_', ' ')} "
                            f"(supported values: {', '.join(catalog[name])})")
        elif money(value) < money(terms[name]):
            rejected.append(f"the requested {name.replace('_', ' ')} EUR {value} is lower than the current "
                            f"EUR {terms[name]}; only increases are supported")
    if rejected:
        listed = "; ".join(rejected)
        return Decision("coverage_change", "closed_without_execution", "invalid_input",
                        f"Inadmissible coverage change on {policy.get('policy_id')}: {listed}.",
                        "The coverage change could not be accepted because " + listed
                        + ". Nothing has been changed: a combined change is applied in full or not at all.")

    changes = {name: value for name, value in supplied.items() if terms.get(name) != value}
    if not changes:
        return Decision("coverage_change", "completed", "no_change",
                        f"Requested final coverage values already hold on {policy.get('policy_id')}.",
                        f"Your annual limit is already EUR {terms.get('annual_limit')} and your per-claim deductible "
                        f"EUR {terms.get('deductible')}, so nothing needed to be changed.")

    final = {name: supplied.get(name, terms.get(name)) for name in COVERAGE_FIELDS}
    thresholds = _thresholds(policy)
    above = [f"{name.replace('_', ' ')} EUR {final[name]} is above the autonomous level EUR {thresholds[name]}"
             for name in COVERAGE_FIELDS if money(final[name]) > money(thresholds[name])]
    effect = {"tool": "update_coverage", "args": {"policy_id": policy["policy_id"], "changes": changes}}
    if above:
        listed = " and ".join(above)
        return Decision("coverage_change", "awaiting_authorization", "authorization_requested",
                        f"Insurer authorization required on {policy.get('policy_id')}: {listed}.",
                        "Your request has been submitted to the insurer for authorization because " + listed
                        + ". Nothing has been changed yet; we will inform you of the decision.",
                        proposal={**effect, "details": {
                            "operation": "coverage_change", "policy_id": policy["policy_id"],
                            "current_values": {name: terms.get(name) for name in COVERAGE_FIELDS},
                            "final_values": final, "changed_fields": sorted(changes),
                            "reason": listed, "evidence": "policyholder request and get_policy current terms"}})
    return Decision("coverage_change", "completed", "updated",
                    "Coverage change admissible and below the authorization levels.",
                    "Applying the requested coverage change.", effect=effect)


def decide_termination(intent, policy, workflow):
    if intent.termination_question or not intent.explicit_termination:
        return decide_enquiry(intent, policy, termination_context=True)
    if _status(policy) == "terminated":
        return Decision("policy_termination", "completed", "no_change",
                        f"Policy {policy.get('policy_id')} is already terminated.",
                        "Your policy is already terminated, so there is nothing further to do.")
    if _status(policy) != "active":
        return _inactive("policy_termination", policy)
    return Decision("policy_termination", "awaiting_authorization", "authorization_requested",
                    f"Termination of {policy.get('policy_id')} always requires insurer authorization.",
                    "We have recorded your instruction to terminate the policy and submitted it to the insurer for "
                    "authorization. The policy is still active until the insurer decides.",
                    proposal={"tool": "terminate_policy", "args": {"policy_id": policy["policy_id"]},
                              "details": {"operation": "policy_termination", "policy_id": policy["policy_id"],
                                          "current_status": _status(policy), "requested_status": "terminated",
                                          "reason": "Explicit policyholder instruction to terminate.",
                                          "evidence": "policyholder request text"}})


def decide_insurer_decision(proposal, event, policy, request_category):
    """Resume after the single insurer decision, bound to the stored proposal."""
    tool = proposal["tool"]
    outcome = {"update_coverage": "updated", "terminate_policy": "terminated"}[tool]
    if event.get("decision") == "reject":
        reason = event.get("reason") or "No reason supplied."
        return Decision(request_category, "closed_without_execution", "insurer_rejected",
                        f"The insurer rejected the proposal for {policy.get('policy_id')}: {reason}",
                        "The insurer did not authorize this request, so nothing has been changed. "
                        f"Reason given: {reason}")
    if tool == "terminate_policy" and _status(policy) == "terminated":
        return Decision(request_category, "completed", "no_change",
                        f"Policy {policy.get('policy_id')} is already terminated.",
                        "Your policy was already terminated, so no further change was needed.")
    if _status(policy) != "active":
        return _inactive(request_category, policy)
    return Decision(request_category, "completed", outcome,
                    f"Insurer authorization received; executing the frozen proposal {tool}.",
                    "The insurer authorized the request; applying it now.",
                    effect={"tool": tool, "args": proposal["args"]})


def describe_effect(tool, data, notes=()):
    """Message built from the actual tool result, never from the intended effect."""
    extra = (" " + " ".join(notes)) if notes else ""
    if tool == "update_contact_details":
        before, after = data["before"], data["after"]
        parts = []
        for name in data["changed_fields"]:
            parts.append(f"{name}: {_contact_value(before.get(name))} -> {_contact_value(after.get(name))}")
        return "Your contact details have been updated. " + "; ".join(parts) + "." + extra
    if tool == "update_coverage":
        before, after = data["before"], data["after"]
        parts = [f"{name.replace('_', ' ')}: EUR {before.get(name)} -> EUR {after.get(name)}"
                 for name in data["changed_fields"]]
        return ("Your coverage has been updated. " + "; ".join(parts)
                + f". Your remaining annual limit is now EUR {after.get('remaining_limit')}."
                + " A higher deductible means a higher share of each invoice is paid by you." * (
                    "deductible" in data["changed_fields"]) + extra)
    if tool == "terminate_policy":
        return (f"Your policy {data['policy_id']} has been terminated after the insurer's authorization "
                f"(status {data['before_status']} -> {data['after_status']})." + extra)
    return "The requested operation completed." + extra


def _contact_value(value):
    if isinstance(value, dict):
        return f"{value.get('street')}, {value.get('postal_code')} {value.get('city')}, {value.get('country')}"
    return str(value)
