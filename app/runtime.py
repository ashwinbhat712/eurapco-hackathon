"""Supervisor-driven runtime: classify, then dispatch to exactly one subagent (no agent handoff).

Still handles initial requests only; human reply/resumption is a later step.
"""

import sys

from app.contracts import validator
from app.claims_extraction import extract_claim
from app.claims_workflow import ReimbursementWorkflow
from app.llm import make_client
from app.mcp_client import DomainClient
from app.subagents import SubagentRun
from app.supervisor import SupervisorUnavailable, classify


def classification_hint(decision):
    return (f"Supervisor routing: this request is classified as request_category={decision.request_category}. "
            "Use exactly this request_category in your final JSON unless it is clearly impossible "
            "for the operation the policyholder asked for.")


def record_final(log, final):
    log.event("model_response", response=final)
    if final["agent_state"] == "awaiting_clarification":
        log.human_requested("clarification", {"question": final["question"]})
    elif final["agent_state"] == "awaiting_authorization":
        log.human_requested("authorization", final["proposal"])
    log.status(agent_state=final["agent_state"], request_outcome=final["request_outcome"],
               reason=final["reason"], message=final["message"], request_category=final["request_category"])


async def dispatch(request, run_id, log, client, decision):
    """Run the supervisor's chosen subagent; its answer is final, even when it is out of scope.

    New reimbursement requests skip the LLM subagent: the deterministic Claims engine decides and only
    uses the model to extract document facts. If the Claims LLM agent itself recognizes a new
    reimbursement (supervisor said otherwise), it hands the same request to that workflow.
    """
    log.route(decision.agent, source="supervisor", request_category=decision.request_category,
              rationale=decision.rationale, attempts=decision.attempts)
    async with DomainClient(run_id, decision.agent, log) as domain:
        if decision.agent == "claims" and decision.request_category == "reimbursement_request":
            final = await reimbursement(request, client, domain, log)
        else:
            subagent = SubagentRun(decision.agent, client, domain)
            final = await subagent.run(request, classification_hint(decision))
            if decision.agent == "claims" and final["request_category"] == "reimbursement_request":
                log.event("model_response", source="claims", response=final, handled_by="claims_engine")
                final = await reimbursement(request, client, domain, log)
    if final["request_category"] != decision.request_category:
        log.event("model_response", source="supervisor", classification_mismatch={
            "supervisor": decision.request_category, "subagent": final["request_category"]})
    return final


async def reimbursement(request, client, domain, log):
    workflow = ReimbursementWorkflow(domain, lambda req: extract_claim(req, client), log)
    return await workflow.run(request)


async def run_request(request, run_id, log, provider=None):
    validator("events.schema.json").validate(request)
    if request["event_type"] != "request":
        raise ValueError("The runtime handles initial requests only; human reply/resumption is not implemented yet.")
    client = make_client(provider)
    try:
        try:
            decision = await classify(request, client)
        except SupervisorUnavailable as exc:
            # No guessed routing: the CLI records failed/technical_failure with null metadata.
            log.event("error", source="supervisor", message=str(exc))
            raise
        log.event("model_response", source="supervisor", routing=decision.as_dict())
        print(f"Supervisor: {decision.agent} / {decision.request_category} "
              f"(attempts={decision.attempts}) - {decision.rationale}", file=sys.stderr)
        final = await dispatch(request, run_id, log, client, decision)
        record_final(log, final)
        return final
    finally:
        if provider is None:
            await client.a_client.close()
