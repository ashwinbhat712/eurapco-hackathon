"""Run the baseline or verify supplied infrastructure."""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

from app.contracts import ROOT, validator
from app.domain import DomainStore
from app.evaluation import EvaluationLog, EvaluationLogError
from app.mcp_client import DomainClient


class CliError(Exception):
    """A condition the participant can fix; the message is written by this kit and safe to print."""


def root_causes(exc):
    """Leaf exceptions. The MCP client wraps failures raised inside it in nested ExceptionGroups."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in root_causes(inner)]
    return [exc]


def cause_names(exc):
    return " + ".join(sorted({type(cause).__name__ for cause in root_causes(exc)}))


async def check():
    store = DomainStore()
    run_id = store.start_run()
    async with DomainClient(run_id, "administration") as domain:
        before = await domain.call("get_policy", {"policy_id": "POL-001"})
        changed = await domain.call("update_contact_details", {"policy_id": "POL-001", "changes": {"phone": "+393339998888"}})
        if not before["ok"] or not changed["ok"] or not changed["data"]["changed"]:
            raise RuntimeError("MCP read/write simulation failed.")
    print("OK: nine tools discovered; isolated MCP read and simulated write completed.")
    print("This check uses no model and does not certify a completed challenge solution.")


def require_api_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CliError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.")


def print_result(engine, result, ticket_id):
    workflow = result.workflow
    summary = {"verdict": result.verdict, "ticket_id": ticket_id}
    summary.update({"state": workflow.state, "outcome": workflow.outcome, "category": workflow.request_category,
                    "reason": workflow.reason, "message": workflow.message} if workflow else {})
    print(json.dumps(summary, indent=2, default=str))
    if workflow:
        print(f"Evaluation JSON: {engine.deps.log_path(workflow.run_id)}")
        print(f"Domain run: {workflow.run_id}")


async def handle(path, fresh=False, interactive=True):
    """Any event (request, clarification reply, insurer decision) through the workflow harness.

    Interactive: while the ticket waits for a clarification or an insurer decision, ask for it and continue.
    """
    event = json.loads(path.read_text(encoding="utf-8"))
    validator("events.schema.json").validate(event)
    require_api_key()
    from app.cli_prompts import next_event
    from app.live.wiring import build_engine
    from app.llm import make_client
    client = make_client()
    try:
        engine = build_engine(client, fresh=fresh)
        while event:
            result = await engine.handle(event)
            print_result(engine, result, event["ticket_id"])
            event = next_event(result.workflow) if interactive else None
    finally:
        await client.a_client.close()


async def run(path):
    request = json.loads(path.read_text(encoding="utf-8"))
    validator("events.schema.json").validate(request)
    if request["event_type"] != "request":
        raise CliError("This baseline runs initial requests only. Reply events are not admitted or resumed: "
                       "use `python -m app handle <event.json>` for the full workflow (see docs/contracts.md).")
    require_api_key()
    from app.runtime import run_request
    store = DomainStore()
    run_id = store.start_run()
    log = EvaluationLog(request, run_id, os.environ.get("TEAM_ID", "TEAM-1"))
    try:
        final = await run_request(request, run_id, log)
        print(json.dumps(final, indent=2))
    except Exception as exc:
        # Record exception types (the real causes, not the wrapper), never raw provider bodies/credentials.
        log.event("error", error_type=cause_names(exc))
        if not any(isinstance(cause, EvaluationLogError) for cause in root_causes(exc)):
            log.status(agent_state="failed", request_outcome="technical_failure", reason=cause_names(exc))
        raise
    finally:
        log.snapshot(store.snapshot(run_id))
        print(f"Evaluation JSON: {log.path}")
        print(f"Domain run: {run_id}")


def validate_log(path):
    record = json.loads(path.read_text(encoding="utf-8"))
    errors = list(validator("evaluation.schema.json").iter_errors(record))
    if errors:
        fields = sorted({".".join(map(str, error.absolute_path)) or "record" for error in errors})
        print("ERROR: evaluation log has missing or invalid fields: " + ", ".join(fields), file=sys.stderr)
        return 1
    print("OK: evaluation log matches the common schema.")
    return 0


def main():
    from pathlib import Path
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Check MCP discovery/read/write without an API key.")
    command = sub.add_parser("run", help="Run one independent initial ticket through the real baseline.")
    command.add_argument("ticket", type=Path)
    command = sub.add_parser("handle", help="Handle any event (request, reply, insurer decision) via the workflow.")
    command.add_argument("event", type=Path)
    command.add_argument("--fresh", action="store_true", help="Rerun a finished ticket in a new domain run.")
    command.add_argument("--no-input", action="store_true",
                         help="Do not prompt for clarification/authorization (default when stdin is not a TTY).")
    command = sub.add_parser("validate-log", help="Validate a cumulative evaluation JSON file.")
    command.add_argument("path", type=Path)
    args = parser.parse_args()
    commands = {
        "check": lambda: asyncio.run(check()),
        "run": lambda: asyncio.run(run(args.ticket)),
        "handle": lambda: asyncio.run(handle(args.event, args.fresh, sys.stdin.isatty() and not args.no_input)),
        "validate-log": lambda: validate_log(args.path),
    }
    try:
        return commands[args.command]() or 0
    except Exception as exc:
        known = [cause for cause in root_causes(exc) if isinstance(cause, (CliError, EvaluationLogError))]
        if known:
            print(f"ERROR: {known[0]}", file=sys.stderr)
        else:
            print(f"ERROR ({cause_names(exc)}): check configuration, input and the documented baseline limitations. "
                  "The evaluation JSON, when written, records the failure.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
