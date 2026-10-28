"""Deterministic stand-ins for the router, agents, rules and MCP client. No model, no network."""

import asyncio
import copy
from collections.abc import Callable
from typing import Any

from pydantic import Field

from app.contracts import ACCESS
from app.domain import DomainStore, error
from app.evaluation import EvaluationLog
from app.workflow.engine import HarnessDeps, WorkflowEngine
from app.workflow.ports import (Action, AuthorizationPlan, Call, Decision, Findings, InformationCheck,
                                RouteResult)
from app.workflow.schema import RuntimeModel
from app.workflow.states import Agent, Outcome
from app.workflow.store import WorkflowStore

POLICY = "POL-001"
TIME = "2026-10-22T10:00:00+02:00"
LATER = "2026-10-22T10:05:00+02:00"


class Events:
    """Valid events for one ticket."""

    @staticmethod
    def request(ticket="T90", event_id=None, text="Please change my phone number.", timestamp=TIME, **extra):
        return {"event_type": "request", "event_id": event_id or f"EV-{ticket}-01", "ticket_id": ticket,
                "policy_id": POLICY, "author_name": "Taylor Example", "timestamp": timestamp, "text": text, **extra}

    @staticmethod
    def reply(ticket="T90", event_id=None, text="Here it is.", timestamp=LATER, **extra):
        return {"event_type": "clarification_reply", "event_id": event_id or f"EV-{ticket}-02", "ticket_id": ticket,
                "timestamp": timestamp, "text": text, **extra}

    @staticmethod
    def decision(ticket="T90", verdict="approve", event_id=None, timestamp=LATER, reason=None):
        event = {"event_type": "insurer_decision", "event_id": event_id or f"EV-{ticket}-03", "ticket_id": ticket,
                 "timestamp": timestamp, "decision": verdict}
        return {**event, "reason": reason} if reason else event


class Calls:
    """Valid write calls on POLICY."""

    @staticmethod
    def phone(phone="+393339998888"):
        return Call(tool="update_contact_details", args={"policy_id": POLICY, "changes": {"phone": phone}})

    @staticmethod
    def coverage(limit="10000.00"):
        return Call(tool="update_coverage", args={"policy_id": POLICY, "changes": {"annual_limit": limit}})

    @staticmethod
    def invoice(number="INV-9", invoice_date="2026-10-21", service_date="2026-10-20", expense="2500.00"):
        return {"provider_id": "PRV-9", "invoice_number": number, "invoice_date": invoice_date,
                "service_date": service_date, "expense": expense, "currency": "EUR"}

    @classmethod
    def create_claim(cls, **invoice_overrides):
        return Call(tool="create_claim", args={"policy_id": POLICY, "patient_name": "Taylor Example",
                                               "service_category": "specialist_consultation",
                                               "invoice": cls.invoice(**invoice_overrides),
                                               "evidence_refs": ["request.invoice_text"]})


class Decisions:
    """Scripted rule outputs."""

    @staticmethod
    def execute(*calls, outcome=Outcome.UPDATED):
        return Decision(action=Action.EXECUTE, outcome=outcome, reason="Allowed autonomously.", calls=calls)

    @staticmethod
    def ask(on_approve, on_reject=(), calls=(), outcome=Outcome.UPDATED, summary="Proposal"):
        plan = AuthorizationPlan(summary=summary, outcome=outcome, on_approve=on_approve, on_reject=on_reject)
        return Decision(action=Action.ASK_AUTHORIZATION, reason="Needs the insurer.", calls=calls, authorization=plan)

    @classmethod
    def claim_over_threshold(cls):
        """Claims > 1500: register + mark pending before asking; pay on approve, record rejection on reject."""
        claim_id = Call.ref(0, "claim_id")
        pending = Call(tool="record_claim_outcome", args={"policy_id": POLICY, "claim_id": claim_id,
                                                          "outcome": "pending_authorization",
                                                          "reason": "Awaiting insurer."})
        pay = Call(tool="pay_claim", args={"policy_id": POLICY, "claim_id": claim_id, "amount": "1960.00"})
        reject = Call(tool="record_claim_outcome", args={"policy_id": POLICY, "claim_id": claim_id,
                                                         "outcome": "rejected", "reason": "placeholder"})
        prep = (Calls.create_claim(), pending)
        return cls.ask((pay,), (reject,), prep, Outcome.PAID, "Pay EUR 1960.00")

    @staticmethod
    def missing(*items):
        return InformationCheck(missing=items, question="Please send: " + ", ".join(items) + ".")


class FakeRouter:
    def __init__(self, agent=Agent.ADMINISTRATION, hint=None):
        self.result, self.texts = RouteResult(agent=agent, category_hint=hint), []

    def route(self, text):
        self.texts.append(text)
        return self.result


class FakeAgent:
    """Scripted findings (the last one repeats). Proposals echo the expected call unless scripted."""

    def __init__(self, *findings, proposals=None, message="Done.", message_error=None, propose_error_at=None):
        self.findings = list(findings) or [Findings(category="contact_update", facts={"phone": "+393339998888"})]
        self.proposals, self.message, self.message_error = list(proposals or []), message, message_error
        self.propose_error_at = propose_error_at
        self.contexts, self.proposed, self.composed = [], [], []

    async def gather(self, context, tools):
        self.contexts.append(context)
        return self.findings.pop(0) if len(self.findings) > 1 else self.findings[0]

    async def propose_write(self, workflow, expected, error, tools):
        self.proposed.append((expected, error))
        if self.propose_error_at is not None and len(self.proposed) - 1 == self.propose_error_at:
            raise RuntimeError("model crashed")
        return self.proposals.pop(0) if self.proposals else expected

    async def compose_message(self, workflow, results):
        self.composed.append(results)
        if self.message_error:
            raise self.message_error
        return self.message


class FakeRules:
    def __init__(self, decision=None, checks=()):
        self.decision = decision or Decisions.execute(Calls.phone())
        self.checks, self.check_calls, self.decide_calls = list(checks), 0, 0

    def check_information(self, workflow):
        self.check_calls += 1
        if not self.checks:
            return InformationCheck()
        return self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]

    async def decide(self, workflow, reader):
        self.decide_calls += 1
        return self.decision(workflow) if callable(self.decision) else self.decision


class DomainRecorder(RuntimeModel):
    """Shared by every client the engine opens. `faults[tool]` = results returned instead of calling."""

    store: DomainStore
    calls: list[tuple[str, dict[str, Any]]] = Field(default_factory=list)
    faults: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    before_call: dict[str, Callable] = Field(default_factory=dict)  # tool -> hook(name, args), e.g. crash image.

    def client(self, run_id, agent, log):
        return FakeDomainClient(self, run_id, agent, log)


class FakeDomainClient:
    def __init__(self, recorder, run_id, agent, log):
        self.recorder, self.run_id, self.agent, self.log = recorder, run_id, agent, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def model_tools(self):
        return [{"name": name} for name in ACCESS[self.agent]]

    async def call(self, name, arguments):
        self.log.event("tool_call", name=name, arguments={"run_id": self.run_id, **arguments})
        self.recorder.calls.append((name, copy.deepcopy(arguments)))
        if name in self.recorder.before_call:
            self.recorder.before_call[name](name, arguments)
        queued = self.recorder.faults.get(name)
        if name not in ACCESS[self.agent]:
            result = error("INVALID_ARGUMENT", "Tool is not available to this agent.")
        elif queued:
            result = queued.pop(0)
        else:
            result = self.recorder.store.call(name, {**arguments, "run_id": self.run_id})
        self.log.event("tool_result", name=name, result=result)
        return result


class Harness:
    """An engine wired to fakes in a temporary directory."""

    def __init__(self, tmp_path, router=None, agents=None, rules=None, fresh=False):
        self.tmp = tmp_path
        self.recorder = DomainRecorder(store=DomainStore(tmp_path / "domain"))
        self.router, self.rules = router or FakeRouter(), rules or FakeRules()
        self.agents = agents or {Agent.ADMINISTRATION: FakeAgent(), Agent.CLAIMS: FakeAgent()}
        self.deps = HarnessDeps(router=self.router, agents=self.agents, rules=self.rules,
                                store=WorkflowStore(tmp_path / "workflows"), domain_store=self.recorder.store,
                                open_domain=self.recorder.client, log_dir=tmp_path / "logs", fresh=fresh)
        self.engine = WorkflowEngine(self.deps)

    def send(self, event):
        return asyncio.run(self.engine.handle(event))

    def workflow(self, ticket="T90"):
        return self.deps.store.load(ticket)

    def log(self, ticket="T90"):
        return EvaluationLog.load(self.deps.log_path(self.workflow(ticket).run_id)).record

    def snapshot(self, ticket="T90"):
        return self.recorder.store.snapshot(self.workflow(ticket).run_id)

    def writes(self, tool=None):
        from app.workflow.proposal import WRITE_TOOLS
        return [(name, args) for name, args in self.recorder.calls if name in WRITE_TOOLS and name == (tool or name)]

    def events(self, kind, ticket="T90"):
        return [event["data"] for event in self.log(ticket)["events"] if event["kind"] == kind]
