"""Fakes for the Part 1 harness: real domain store and logger, scripted router/agents/rules. No API key."""

import asyncio
import copy
from collections import deque

import pytest

from app.contracts import ACCESS, tool_schema
from app.domain import DomainStore
from app.evaluation import EvaluationLog
from app.workflow.engine import HarnessDeps, WorkflowEngine
from app.workflow.ports import Action, AuthorizationPlan, Call, Decision, Findings, InformationCheck, RouteResult
from app.workflow.states import Agent, Outcome
from app.workflow.store import WorkflowStore


class SimulatedCrash(BaseException):
    """Stands in for a killed process: not an Exception, so the harness cannot handle it."""


class InProcessDomain:
    """DomainClient without MCP: same run binding, agent scoping and tool_call/tool_result logging."""

    def __init__(self, factory, run_id, agent, log):
        self.factory, self.run_id, self.agent, self.log = factory, run_id, agent, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def model_tools(self):
        return [{"name": name, "input_schema": tool_schema(name, model_facing=True)} for name in ACCESS[self.agent]]

    async def call(self, name, arguments):
        self.factory.calls.append((name, copy.deepcopy(arguments)))
        self.log.event("tool_call", name=name, arguments={"run_id": self.run_id, **arguments})
        if name in self.factory.raise_before:
            raise self.factory.raise_before.pop(name)
        if name in self.factory.fail_with:
            result = self.factory.fail_with.pop(name)
            self.log.event("tool_result", name=name, result=result)
            return result
        result = self.factory.store.call(name, {**arguments, "run_id": self.run_id})
        self.log.event("tool_result", name=name, result=result)
        if name in self.factory.raise_after:
            raise self.factory.raise_after.pop(name)
        return result


class DomainFactory:
    def __init__(self, store):
        self.store, self.calls, self.raise_before, self.raise_after = store, [], {}, {}
        self.fail_with = {}  # tool -> error result returned instead of calling the store.

    def __call__(self, run_id, agent, log):
        return InProcessDomain(self, run_id, agent, log)

    def count(self, name):
        return sum(1 for called, _ in self.calls if called == name)


class ScriptedRouter:
    def __init__(self):
        self.result, self.texts = RouteResult(agent=Agent.ADMINISTRATION), []

    async def route(self, request):
        self.texts.append(request["text"])
        return self.result


class ScriptedAgent:
    """gather() pops scripted findings; propose_write() echoes the expected call unless scripted."""

    def __init__(self):
        self.findings, self.proposals, self.contexts, self.proposed = deque(), deque(), [], []
        self.message, self.message_error = "Done.", None

    async def gather(self, context, tools):
        self.contexts.append(context)
        return self.findings.popleft() if len(self.findings) > 1 else self.findings[0]

    async def propose_write(self, workflow, expected, error, tools):
        self.proposed.append((expected, error, [tool["name"] for tool in tools.model_tools()]))
        return self.proposals.popleft()(expected, error) if self.proposals else expected

    async def compose_message(self, workflow, results):
        if self.message_error:
            raise self.message_error
        return self.message


class ScriptedRules:
    def __init__(self):
        self.checks, self.decisions, self.decide_calls = deque([InformationCheck()]), deque(), 0

    def check_information(self, workflow):
        return self.checks.popleft() if len(self.checks) > 1 else self.checks[0]

    async def decide(self, workflow, reader):
        self.decide_calls += 1
        return self.decisions.popleft() if len(self.decisions) > 1 else self.decisions[0]


class Harness:
    TIME = "2026-10-22T10:{minute:02d}:00+02:00"

    def __init__(self, tmp_path):
        self.domain_store = DomainStore(tmp_path / "domain")
        self.domain = DomainFactory(self.domain_store)
        self.router, self.rules = ScriptedRouter(), ScriptedRules()
        self.agents = {Agent.ADMINISTRATION: ScriptedAgent(), Agent.CLAIMS: ScriptedAgent()}
        self.store = WorkflowStore(tmp_path / "workflows")
        self.log_dir = tmp_path / "logs"
        self.fresh = False
        self._minute = 0

    @property
    def admin(self):
        return self.agents[Agent.ADMINISTRATION]

    @property
    def claims(self):
        return self.agents[Agent.CLAIMS]

    def engine(self):
        return WorkflowEngine(HarnessDeps(router=self.router, agents=self.agents, rules=self.rules, store=self.store,
                                          domain_store=self.domain_store, open_domain=self.domain,
                                          log_dir=self.log_dir, fresh=self.fresh))

    def send(self, event):
        return asyncio.run(self.engine().handle(event))

    def _stamp(self, timestamp):
        self._minute += 1
        return timestamp or self.TIME.format(minute=self._minute)

    def request(self, text="Please change my phone number to +393339998888", ticket="T1", event_id=None,
                timestamp=None, **extra):
        return {"event_type": "request", "event_id": event_id or f"EV-{ticket}-01", "ticket_id": ticket,
                "policy_id": "POL-001", "author_name": "Taylor Example", "timestamp": self._stamp(timestamp),
                "text": text, **extra}

    def reply(self, text="The postal code is 10100.", ticket="T1", event_id="EV-T1-02", timestamp=None, **extra):
        return {"event_type": "clarification_reply", "event_id": event_id, "ticket_id": ticket,
                "timestamp": self._stamp(timestamp), "text": text, **extra}

    def decision(self, decision="approve", ticket="T1", event_id="EV-T1-03", timestamp=None, reason=None):
        event = {"event_type": "insurer_decision", "event_id": event_id, "ticket_id": ticket,
                 "timestamp": self._stamp(timestamp), "decision": decision}
        return {**event, "reason": reason} if reason else event

    def workflow(self, ticket="T1"):
        return self.store.load(ticket)

    def log(self, ticket="T1"):
        return EvaluationLog.load(self.log_dir / f"{self.workflow(ticket).run_id}.json").record

    def policy(self, ticket="T1", policy_id="POL-001"):
        return self.domain_store.snapshot(self.workflow(ticket).run_id)["policies"][policy_id]

    def events(self, kind, ticket="T1"):
        return [event for event in self.log(ticket)["events"] if event["kind"] == kind]

    # Common scripts.
    def phone_update(self, category="contact_update", phone="+393331112222"):
        self.admin.findings.append(Findings(category=category, facts={"phone": phone}))
        call = Call(tool="update_contact_details", args={"policy_id": "POL-001", "changes": {"phone": phone}})
        self.rules.decisions.append(Decision(action=Action.EXECUTE, outcome=Outcome.UPDATED,
                                             reason="Contact update allowed.", calls=(call,)))

    @staticmethod
    def create_claim(expense="1600.00", invoice_date="2026-10-12", service_date="2026-10-10"):
        return Call(tool="create_claim", args={
            "policy_id": "POL-001", "patient_name": "Taylor Example", "service_category": "specialist_consultation",
            "invoice": {"provider_id": "PROVIDER-001", "invoice_number": "INV-T-900", "invoice_date": invoice_date,
                        "service_date": service_date, "expense": expense, "currency": "EUR"},
            "evidence_refs": ["invoice_text"]})

    @staticmethod
    def claim_outcome(outcome, reason, index=0):
        return Call(tool="record_claim_outcome", args={"policy_id": "POL-001", "claim_id": Call.ref(index, "claim_id"),
                                                       "outcome": outcome, "reason": reason})

    def claim_route(self):
        self.router.result = RouteResult(agent=Agent.CLAIMS, category_hint="reimbursement_request")
        self.claims.findings.append(Findings(category="reimbursement_request", facts={"expense": "1600.00"}))

    @staticmethod
    def pay(amount="1560.00"):
        return Call(tool="pay_claim", args={"policy_id": "POL-001", "claim_id": Call.ref(0, "claim_id"),
                                            "amount": amount})

    def small_claim(self, **invoice):
        self.claim_route()
        self.rules.decisions.append(Decision(action=Action.EXECUTE, outcome=Outcome.PAID, reason="Covered.",
                                             calls=(self.create_claim(**invoice), self.pay())))

    def large_claim(self):
        """Plan §15: register, mark pending_authorization, then ask; pay or reject only after the decision."""
        self.claim_route()
        plan = AuthorizationPlan(summary="Pay 1560.00 EUR on the new claim.", outcome=Outcome.PAID,
                                 on_approve=(self.pay(),),
                                 on_reject=(self.claim_outcome("rejected", "Insurer decision pending."),))
        self.rules.decisions.append(Decision(
            action=Action.ASK_AUTHORIZATION, outcome=Outcome.AUTHORIZATION_REQUESTED,
            reason="Above the authorization threshold.",
            calls=(self.create_claim(), self.claim_outcome("pending_authorization", "Above 1500.00 EUR.")),
            authorization=plan))

    def claims_on_policy(self, invoice_number="INV-T-900"):
        claims = self.domain_store.snapshot(self.workflow().run_id)["claims"].values()
        return [claim for claim in claims if claim["invoice"]["invoice_number"] == invoice_number]


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def crash():
    return SimulatedCrash
