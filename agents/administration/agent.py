"""Administration agent: deterministic executor around the Part 4 rules.

`system_prompt()` is kept so the supplied baseline keeps working. `handle()` is the
entry point for the workflow harness: it retrieves a fresh policy, lets `rules.py`
decide, and then either performs exactly one effect, asks the one clarification or
freezes one proposal for the insurer.
"""

import copy

from agents.administration import rules
from agents.administration.extraction import normalize_money
from agents.administration.ports import ProposalIntegrityError, build_proposal, verify_proposal
from app.contracts import ROOT, validator

MAX_CORRECTIONS = 3
MONEY_FIELDS = ("annual_limit", "deductible", "amount", "expense")
BUSINESS_ERROR_CODES = ("NOT_FOUND", "REFERENCE_MISMATCH", "INVALID_STATE", "OPERATION_FAILED")


def system_prompt():
    return (ROOT / "agents" / "administration" / "prompt.md").read_text(encoding="utf-8")


def response(decision, *, question=None, proposal=None, message=None):
    payload = {"request_category": decision.request_category, "agent_state": decision.agent_state,
               "request_outcome": decision.request_outcome, "reason": decision.reason,
               "message": message or decision.message, "question": question, "proposal": proposal}
    validator("agent-response.schema.json").validate(payload)
    return payload


async def handle(event, workflow, domain, store, log, extractor=None):
    """Handle one administration event and return a schema-valid agent response."""
    if getattr(log, "record", {}).get("handling_agent") is None:
        log.route("administration")

    policy_result = await domain.call("get_policy", {"policy_id": workflow.policy_id})
    if not policy_result["ok"]:
        return _technical(log, workflow, "get_policy failed: " + policy_result["error"]["message"])
    policy = policy_result["data"]

    if workflow.phase == "insurer_decision":
        decision = _resume(event, workflow, store, policy, log)
        if decision is None:
            return _technical(log, workflow, "No intact frozen proposal is available for this ticket.")
    else:
        intent = await extractor.extract(event, workflow)
        log.event("model_response", extracted_intent=_intent_payload(intent))
        decision = rules.decide(intent, policy, workflow)

    workflow.facts["request_category"] = decision.request_category
    return await _apply(decision, workflow, domain, store, log)


def _intent_payload(intent):
    return {"request_category": intent.request_category, "contact_fields": intent.contact_fields,
            "email": intent.email, "phone": intent.phone, "address": intent.address,
            "coverage_fields": intent.coverage_fields, "annual_limit": intent.annual_limit,
            "deductible": intent.deductible, "explicit_termination": intent.explicit_termination,
            "termination_question": intent.termination_question, "summary": intent.summary}


def _resume(event, workflow, store, policy, log):
    proposal = store.load(workflow.ticket_id)
    if proposal is None:
        return None
    try:
        verify_proposal(proposal)
    except ProposalIntegrityError as exc:
        log.event("error", error_type=type(exc).__name__, message=str(exc))
        return None
    category = workflow.facts.get("request_category") or proposal.get("details", {}).get("operation")
    return rules.decide_insurer_decision(proposal, event, policy, category)


async def _apply(decision, workflow, domain, store, log):
    if decision.question:
        log.human_requested("clarification", {"question": decision.question})
        workflow.clarification_issued()
        final = response(decision, question=decision.question)
    elif decision.proposal:
        proposal = build_proposal(decision.proposal["tool"], decision.proposal["args"],
                                  details=decision.proposal.get("details", {}))
        store.save(workflow.ticket_id, proposal)
        log.human_requested("authorization", proposal)
        final = response(decision, proposal=proposal)
    elif decision.effect:
        final = await _execute(decision, workflow, domain, log)
    else:
        final = response(decision)
    log.status(agent_state=final["agent_state"], request_outcome=final["request_outcome"],
               reason=final["reason"], message=final["message"], request_category=final["request_category"])
    return final


async def _execute(decision, workflow, domain, log):
    tool = decision.effect["tool"]
    arguments = copy.deepcopy(decision.effect["args"])
    while True:
        result = await domain.call(tool, arguments)
        if result["ok"]:
            return response(decision, message=rules.describe_effect(tool, result["data"], decision.notes))
        failure = result["error"]
        if failure.get("code") != "INVALID_ARGUMENT" or not failure.get("retryable"):
            return _rejected(decision, failure)
        signature = f"{tool}:{failure.get('code')}:{failure.get('field', '')}"
        attempts = workflow.record_attempt(signature)
        corrected = correct_arguments(arguments, failure)
        if attempts > MAX_CORRECTIONS or corrected is None or corrected == arguments:
            log.event("handover", tool=tool, attempts=attempts, error=failure,
                      summary=f"{tool} kept failing with {failure.get('code')} on "
                              f"{failure.get('field', 'the payload')}; the correction attempts are exhausted.")
            return response(rules.Decision(decision.request_category, "closed_without_execution", "handover_required",
                                           f"{tool} could not be corrected after {min(attempts, MAX_CORRECTIONS)} "
                                           f"attempts: {failure.get('message')}",
                                           "We could not complete this change automatically and have passed it to a "
                                           "colleague. Nothing has been changed on your policy."))
        log.event("error", error_type="INVALID_ARGUMENT", field=failure.get("field"), attempt=attempts)
        arguments = corrected


def _rejected(decision, failure):
    outcome = "invalid_input" if failure.get("code") in BUSINESS_ERROR_CODES else "technical_failure"
    state = "closed_without_execution" if outcome == "invalid_input" else "failed"
    return response(rules.Decision(decision.request_category, state, outcome,
                                   f"{failure.get('code')}: {failure.get('message')}",
                                   "The requested change could not be applied, so nothing has been modified. "
                                   f"Reason: {failure.get('message')}"))


def _technical(log, workflow, reason):
    decision = rules.Decision(workflow.facts.get("request_category") or "unclear", "failed", "technical_failure",
                              reason, "A technical problem prevented us from handling this request; "
                                      "nothing has been changed.")
    log.status(agent_state="failed", request_outcome="technical_failure", reason=reason,
               message=decision.message, request_category=decision.request_category)
    return response(decision)


def correct_arguments(arguments, failure):
    """Repair only the representation of a value; never the requested facts or action."""
    field = failure.get("field")
    if not field:
        return None
    path = [part for part in field.split(".") if part != "run_id"]
    container, key = _locate(arguments, path)
    if container is None or key not in container:
        return None
    value = container[key]
    candidates = []
    normalized = normalize_money(value) if key in MONEY_FIELDS and isinstance(value, str) else None
    allowed = failure.get("allowed_values")
    if allowed:
        candidates.extend(option for option in allowed if option in (value, normalized))
    if normalized and normalized != value:
        candidates.append(normalized)
    if isinstance(value, str) and value.strip() != value:
        candidates.append(value.strip())
    if not candidates:
        return None
    corrected = copy.deepcopy(arguments)
    _locate(corrected, path)[0][key] = candidates[0]
    return corrected


def _locate(arguments, path):
    container = arguments
    for part in path[:-1]:
        if not isinstance(container, dict) or part not in container:
            return None, None
        container = container[part]
    return (container, path[-1]) if isinstance(container, dict) else (None, None)
