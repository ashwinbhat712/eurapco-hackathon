"""Interactive CLI input for a suspended workflow: the clarification reply or the insurer decision."""

import uuid
from datetime import datetime, timedelta

from app.workflow.states import EventType, State

# Replies are stamped just after the suspension so the workflow timeouts never expire them.
REPLY_DELAY = timedelta(minutes=1)
DECISIONS = {"a": "approve", "approve": "approve", "r": "reject", "reject": "reject"}
SKIP_HELP = ("Skip = leave the ticket waiting (nothing is executed). Resume later with "
             "`python -m app handle <event.json>` and a matching reply event.")


def _calls(calls):
    lines = [f"    - {call.tool}({', '.join(f'{key}={value!r}' for key, value in call.args.items())})"
             for call in calls]
    return "\n".join(lines) or "    - nothing"


def _facts(facts):
    return "\n".join(f"    {key}: {value}" for key, value in facts.items() if value not in (None, "", [], {}))


def _base_event(workflow, event_type):
    suspended = workflow.suspended_at or datetime.fromisoformat(workflow.original_request["timestamp"])
    return {"event_type": event_type, "event_id": f"EV-{workflow.ticket_id}-CLI-{uuid.uuid4().hex[:8]}",
            "ticket_id": workflow.ticket_id, "timestamp": (suspended + REPLY_DELAY).isoformat()}


def _ask_clarification(workflow):
    request = workflow.original_request
    print(f"\n=== Clarification needed for ticket {workflow.ticket_id} (policy {workflow.policy_id}) ===")
    print(f"Customer wrote ({request['author_name']}):\n    {request['text']}")
    print(f"Question to the customer:\n    {workflow.clarification.question}")
    print(SKIP_HELP)
    text = input("Customer reply (empty = skip): ").strip()
    return {**_base_event(workflow, EventType.CLARIFICATION_REPLY), "text": text} if text else None


def _print_authorization(workflow):
    request, proposal = workflow.original_request, workflow.proposal
    print(f"\n=== Insurer authorization needed for ticket {workflow.ticket_id} (policy {workflow.policy_id}) ===")
    print(f"Customer wrote ({request['author_name']}):\n    {request['text']}")
    print(f"Why authorization is needed:\n    {workflow.reason}")
    print(f"Proposal {proposal.proposal_id}:\n    {proposal.summary}")
    print(f"Facts used:\n{_facts(workflow.facts) or '    -'}")
    print(f"If you APPROVE, the system executes exactly (outcome: {proposal.outcome}):\n{_calls(proposal.on_approve)}")
    print(f"If you REJECT, the system executes:\n{_calls(proposal.on_reject)}")
    print(SKIP_HELP)


def _ask_authorization(workflow):
    _print_authorization(workflow)
    answer = input("Decision [a]pprove / [r]eject / empty = skip: ").strip().lower()
    while answer and answer not in DECISIONS:
        answer = input("Type a, r or leave empty to skip: ").strip().lower()
    if not answer:
        return None
    event = {**_base_event(workflow, EventType.INSURER_DECISION), "decision": DECISIONS[answer]}
    if event["decision"] != "reject":
        return event
    reason = ""
    while not reason:  # The event schema requires a reason for a rejection.
        reason = input("Reason (required): ").strip()
    return {**event, "reason": reason}


PROMPTS = {State.AWAITING_CLARIFICATION: _ask_clarification, State.AWAITING_AUTHORIZATION: _ask_authorization}


def next_event(workflow):
    """The event that resumes `workflow`, or None when it is not suspended or the user stops."""
    prompt = PROMPTS.get(workflow.state) if workflow else None
    return prompt(workflow) if prompt else None
