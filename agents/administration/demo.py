"""Local driver for the administration package only.

Not the shared runtime: it exists so the Part 4 rules can be exercised end to end
(request, optional clarification reply, optional insurer decision) against the real
MCP tools and the supplied logger, in one run and one evaluation log.

    python -m agents.administration.demo examples/training/T05.json
    python -m agents.administration.demo examples/training/T04.json --reply examples/events/clarification_reply.json
    python -m agents.administration.demo examples/training/T06.json --decision approve
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from agents.administration.agent import handle
from agents.administration.extraction import ModelExtractor
from agents.administration.ports import InMemoryProposalStore, WorkflowState
from app.contracts import ROOT, validator
from app.domain import DomainStore
from app.evaluation import EvaluationLog
from app.mcp_client import DomainClient


async def drive(request, reply, decision):
    from anthropic import AsyncAnthropic

    store = DomainStore()
    run_id = store.start_run()
    log = EvaluationLog(request, run_id, os.environ.get("TEAM_ID", "TEAM-1"))
    workflow = WorkflowState(ticket_id=request["ticket_id"], run_id=run_id,
                             policy_id=request["policy_id"], facts={"request": request})
    proposals = InMemoryProposalStore()
    provider = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0, timeout=45)
    try:
        async with DomainClient(run_id, "administration", log) as domain:
            extractor = ModelExtractor(provider)
            final = await handle(request, workflow, domain, proposals, log, extractor)
            print(json.dumps(final, indent=2, ensure_ascii=False))
            for event, phase in ((reply, "clarification_reply"), (decision, "insurer_decision")):
                if event is None:
                    continue
                if final["agent_state"] != {"clarification_reply": "awaiting_clarification",
                                            "insurer_decision": "awaiting_authorization"}[phase]:
                    print(f"Skipping the {phase}: the request is not waiting for it.")
                    continue
                log.input(event)
                workflow.phase = phase
                final = await handle(event, workflow, domain, proposals, log, extractor)
                print(json.dumps(final, indent=2, ensure_ascii=False))
    finally:
        await provider.close()
        log.snapshot(store.snapshot(run_id))
        print(f"Evaluation JSON: {log.path}")
        print(f"Domain run: {run_id}")


def load_event(path, ticket_id, expected):
    if path is None:
        return None
    event = json.loads(Path(path).read_text(encoding="utf-8"))
    validator("events.schema.json").validate(event)
    if event["event_type"] != expected:
        raise SystemExit(f"{path} is not a {expected} event.")
    return {**event, "ticket_id": ticket_id}


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket", type=Path)
    parser.add_argument("--reply", type=Path, help="clarification_reply event to resume with")
    parser.add_argument("--decision", choices=("approve", "reject"), help="insurer decision to resume with")
    parser.add_argument("--reason", default="Declined for this training execution.")
    args = parser.parse_args()

    request = json.loads(args.ticket.read_text(encoding="utf-8"))
    validator("events.schema.json").validate(request)
    if request["event_type"] != "request":
        raise SystemExit("The first event must be a request.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.")

    reply = load_event(args.reply, request["ticket_id"], "clarification_reply")
    decision = None
    if args.decision:
        decision = {"event_type": "insurer_decision", "event_id": request["event_id"] + "-INS",
                    "ticket_id": request["ticket_id"], "timestamp": request["timestamp"],
                    "decision": args.decision}
        if args.decision == "reject":
            decision["reason"] = args.reason
    asyncio.run(drive(request, reply, decision))


if __name__ == "__main__":
    main()
