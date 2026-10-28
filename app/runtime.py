"""Supervisor-driven runtime: classify, dispatch to one subagent, re-route once if needed.

Still handles initial requests only; human reply/resumption is a later step.
"""

import sys

from app.contracts import validator
from app.llm import make_client
from app.mcp_client import DomainClient
from app.subagents import SubagentRun
from app.supervisor import CROSS_DOMAIN, OTHER_AGENT, SupervisorUnavailable, classify


def classification_hint(decision):
    return (f"Supervisor routing: this request is classified as request_category={decision.request_category}. "
            "Use exactly this request_category in your final JSON unless it is clearly impossible "
            "for the operation the policyholder asked for.")


def handoff_hint(previous_agent):
    return (f"Supervisor routing: the {previous_agent} agent judged this operation outside its domain and "
            "handed it to you. Classify it with your own request categories.")


def record_final(log, final):
    log.event("model_response", response=final)
    if final["agent_state"] == "awaiting_clarification":
        log.human_requested("clarification", {"question": final["question"]})
    elif final["agent_state"] == "awaiting_authorization":
        log.human_requested("authorization", final["proposal"])
    log.status(agent_state=final["agent_state"], request_outcome=final["request_outcome"],
               reason=final["reason"], message=final["message"], request_category=final["request_category"])


async def dispatch(request, run_id, log, client, decision):
    """Run the chosen subagent; hand over once to the other agent on a cross-domain answer."""
    agent, hint = decision.agent, classification_hint(decision)
    reason = {"source": "supervisor", "request_category": decision.request_category,
              "rationale": decision.rationale, "attempts": decision.attempts}
    for hop in range(2):
        log.route(agent, **reason)
        async with DomainClient(run_id, agent, log) as domain:
            subagent = SubagentRun(agent, client, domain)
            final = await subagent.run(request, hint)
        crossed = final["request_category"] == CROSS_DOMAIN[agent]
        if crossed and hop == 0 and not subagent.successful_writes:
            log.event("model_response", source=agent, handoff_to=OTHER_AGENT[agent], response=final)
            reason = {"source": agent, "request_category": final["request_category"],
                      "rationale": final["reason"]}
            print(f"Handoff: {agent} -> {OTHER_AGENT[agent]} - {final['reason']}", file=sys.stderr)
            agent, hint = OTHER_AGENT[agent], handoff_hint(agent)
            continue
        if final["request_category"] != decision.request_category:
            log.event("model_response", source="supervisor", classification_mismatch={
                "supervisor": decision.request_category, "subagent": final["request_category"]})
        return final


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
