"""Administration rules (docs/policy.md): contact update, coverage change, termination, policy enquiry."""

from app.checks.coverage import CATALOG_KEYS, check_coverage_change
from app.live.common import PolicyStatus, ask, close, get_policy, inactive_policy, no_change
from app.workflow.ports import Action, AuthorizationPlan, Call, Decision, InformationCheck
from app.workflow.states import Outcome

ADDRESS_PARTS = ("street", "city", "postal_code", "country")
LABELS = {
    "contact_details": "the new email, phone number or postal address",
    "street": "the street", "city": "the city", "postal_code": "the postal code", "country": "the country",
    "coverage_values": "the new annual limit and/or deductible",
    "explicit_instruction": "an explicit instruction to terminate the policy",
}


def _check(missing):
    return InformationCheck(missing=tuple(missing), question=ask(LABELS, missing) if missing else "")


def check_contact(workflow):
    facts = workflow.facts
    given = [key for key in ("email", "phone", *ADDRESS_PARTS) if facts.get(key)]
    address = [part for part in ADDRESS_PARTS if not facts.get(part)] if set(given) & set(ADDRESS_PARTS) else []
    return _check(address if given else ["contact_details"])


def check_coverage(workflow):
    return _check([] if any(workflow.facts.get(key) for key in CATALOG_KEYS) else ["coverage_values"])


def check_termination(workflow):
    return _check([] if workflow.facts.get("explicit_instruction") is True else ["explicit_instruction"])


def check_nothing(workflow):
    return InformationCheck()


async def decide_policy_enquiry(workflow, reader, agent):
    policy = await get_policy(reader, workflow)
    message = await agent.compose_message(workflow, [{"ok": True, "data": policy}])
    return Decision(action=Action.ANSWER, outcome=Outcome.ANSWERED, reason="Answered from get_policy.",
                    message=message)


def _contact_changes(facts, contacts):
    changes = {key: facts[key].strip() for key in ("email", "phone")
               if facts.get(key) and facts[key].strip() != contacts.get(key)}
    address = {part: facts[part].strip() for part in ADDRESS_PARTS if facts.get(part)}
    changes.update({"address": address} if address and address != contacts.get("address") else {})
    return changes


async def decide_contact_update(workflow, reader, agent):
    policy = await get_policy(reader, workflow)
    if policy["status"] != PolicyStatus.ACTIVE:
        return inactive_policy(policy)
    changes = _contact_changes(workflow.facts, policy["contacts"])
    if not changes:
        return no_change("The contact details already have these values; nothing was changed.")
    call = Call(tool="update_contact_details", args={"policy_id": workflow.policy_id, "changes": changes})
    return Decision(action=Action.EXECUTE, outcome=Outcome.UPDATED, reason="Contact details change: "
                    + ", ".join(changes), calls=(call,))


async def decide_coverage_change(workflow, reader, agent):
    policy = await get_policy(reader, workflow)
    check = check_coverage_change(policy, annual_limit=workflow.facts.get("annual_limit"),
                                  deductible=workflow.facts.get("deductible"))
    if not check.result.ok:
        return close(check.result.outcome, f"Unsupported coverage change: {check.result.reason} Nothing was changed.")
    if check.no_change:
        return no_change("The coverage already has these values; nothing was changed.")
    changes, terms = check.changes, policy["financial_terms"]
    call = Call(tool="update_coverage", args={"policy_id": workflow.policy_id, "changes": changes})
    text = ", ".join(f"{key} {terms[key]} -> {value}" for key, value in changes.items())
    if check.needs_authorization:
        plan = AuthorizationPlan(summary=f"Coverage change on {workflow.policy_id}: {text}.",
                                 outcome=Outcome.UPDATED, on_approve=(call,))
        return Decision(action=Action.ASK_AUTHORIZATION, outcome=Outcome.UPDATED,
                        reason="Requested values exceed the authorization thresholds.", authorization=plan)
    return Decision(action=Action.EXECUTE, outcome=Outcome.UPDATED, reason=f"Coverage change: {text}.",
                    calls=(call,))


async def decide_termination(workflow, reader, agent):
    policy = await get_policy(reader, workflow)
    if policy["status"] == PolicyStatus.TERMINATED:
        return no_change("The policy is already terminated; nothing was changed.")
    if policy["status"] != PolicyStatus.ACTIVE:
        return inactive_policy(policy)
    call = Call(tool="terminate_policy", args={"policy_id": workflow.policy_id})
    plan = AuthorizationPlan(summary=f"Terminate policy {workflow.policy_id} at the policyholder's request.",
                             outcome=Outcome.TERMINATED, on_approve=(call,))
    return Decision(action=Action.ASK_AUTHORIZATION, outcome=Outcome.TERMINATED,
                    reason="Termination always needs insurer authorization.", authorization=plan)
