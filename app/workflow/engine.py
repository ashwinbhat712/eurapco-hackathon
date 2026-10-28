"""Event entry point (plan §13): admission, timeouts and one handler per event type."""

from collections.abc import Callable
from pathlib import Path

from pydantic import Field

from app.contracts import ROOT, validator
from app.domain import DomainStore
from app.evaluation import EvaluationLog
from app.mcp_client import DomainClient
from app.workflow.admission import Admission, AdmissionPolicy, Verdict
from app.workflow.machine import StateMachine
from app.workflow.phases import Phases
from app.workflow.ports import AgentWorker, Router, Rules
from app.workflow.record import Workflow
from app.workflow.schema import RuntimeModel, ValueModel
from app.workflow.session import Session
from app.workflow.states import PROCESSING_STATES, SUSPENDED, TERMINAL, Agent, EventType, State
from app.workflow.store import WorkflowStore


class HarnessDeps(RuntimeModel):
    router: Router
    agents: dict[Agent, AgentWorker]
    rules: Rules
    store: WorkflowStore = Field(default_factory=WorkflowStore)
    domain_store: DomainStore = Field(default_factory=DomainStore)
    open_domain: Callable = DomainClient  # (run_id, agent, log) -> async context manager
    team_id: str = "TEAM-1"
    log_dir: Path | None = None
    fresh: bool = False  # Development only: rerun a terminal ticket in a new run.

    def log_path(self, run_id):
        return Path(self.log_dir or ROOT / "logs") / f"{run_id}.json"


class HandleResult(ValueModel):
    verdict: Verdict
    workflow: Workflow | None
    admission: Admission | None = None


class WorkflowEngine:
    REPLY_FIELDS = ("text", "invoice_text", "prescription_text")
    REJECTED_ERROR_TYPE = "EventRejected"
    TIMEOUT_NEXT_STEP = "Contact the insurer for a decision and continue manually."

    def __init__(self, deps):
        self.deps = deps
        self.admission = AdmissionPolicy(deps.fresh)
        self.verdicts = {Verdict.ADMIT: self._admit, Verdict.RECOVER: self._recover,
                         Verdict.IGNORE: self._ignore, Verdict.REJECT: self._reject}
        self.handlers = {EventType.REQUEST: self._start, EventType.CLARIFICATION_REPLY: self._resume,
                         EventType.INSURER_DECISION: self._resume}
        self.appliers = {EventType.CLARIFICATION_REPLY: self._apply_reply,
                         EventType.INSURER_DECISION: self._apply_decision}

    async def handle(self, event):
        validator("events.schema.json").validate(event)
        workflow = self.deps.store.load(event["ticket_id"])
        admission = self.admission.admit(event, workflow)
        if admission.verdict == Verdict.ADMIT and self._expire(workflow, event):
            admission = self.admission.admit(event, workflow)  # The late event now meets a terminal workflow.
        return await self.verdicts[admission.verdict](event, workflow, admission)

    async def _admit(self, event, workflow, admission):
        return await self.handlers[EventType(event["event_type"])](event, workflow)

    async def _ignore(self, event, workflow, admission):
        return HandleResult(verdict=Verdict.IGNORE, workflow=workflow, admission=admission)

    async def _reject(self, event, workflow, admission):
        if workflow is not None:  # Evidence on the ticket's own log, without a status change.
            EvaluationLog.load(self.deps.log_path(workflow.run_id)).event(
                "error", error_type=self.REJECTED_ERROR_TYPE, event_id=event["event_id"],
                event_type=event["event_type"], rejection=admission.rejection, state=workflow.state)
        return HandleResult(verdict=Verdict.REJECT, workflow=workflow, admission=admission)

    async def _start(self, event, previous):
        run_id = self.deps.domain_store.start_run()
        workflow = Workflow.start(event, run_id)
        log = EvaluationLog(event, run_id, self.deps.team_id, self.deps.log_dir)
        self.deps.store.save(workflow)
        return await self._drive(Session(self.deps, workflow, log, event["timestamp"]))

    async def _resume(self, event, workflow):
        workflow.begin_event(event["event_id"])
        self.deps.store.save(workflow)
        log = EvaluationLog.load(self.deps.log_path(workflow.run_id))
        log.input(event)
        return await self._drive(Session(self.deps, workflow, log, event["timestamp"]),
                                 self.appliers[EventType(event["event_type"])], event)

    async def _recover(self, event, workflow, admission):
        """Same event after a crash: re-apply it if it never took effect, else continue the saved state."""
        log = EvaluationLog.load(self.deps.log_path(workflow.run_id))
        session = Session(self.deps, workflow, log, event["timestamp"])
        unapplied = SUSPENDED.get(workflow.state) == event["event_type"]
        apply = self.appliers[EventType(event["event_type"])] if unapplied else None
        return await self._drive(session, apply, event)

    async def _apply_reply(self, session, phases, event):
        reply = {key: event[key] for key in self.REPLY_FIELDS if event.get(key)}
        clarification = session.workflow.clarification
        clarification.answered, clarification.reply = True, reply
        session.move(State.GATHERING, reason="Clarification reply received.")

    async def _apply_decision(self, session, phases, event):
        await phases.decide_authorization(event["decision"], event.get("reason"))

    async def _drive(self, session, apply=None, event=None):
        workflow = session.workflow
        try:
            async with session:
                phases = Phases(session)
                if apply is not None:
                    await apply(session, phases, event)
                await phases.run_until_suspended()
                if workflow.state in PROCESSING_STATES:
                    raise RuntimeError(f"Workflow stopped in {workflow.state} without a decision.")
        except Exception as exc:
            if workflow.state not in TERMINAL:
                session.fail(exc)
        except BaseException:
            # Interrupted (Ctrl-C, kill): keep in_flight_event_id so re-sending the same event recovers.
            self.deps.store.save(workflow)
            raise
        workflow.finish_event()
        self.deps.store.save(workflow)
        session.log.snapshot(self.deps.domain_store.snapshot(workflow.run_id))
        return HandleResult(verdict=Verdict.ADMIT, workflow=workflow)

    def _expire(self, workflow, event):
        """Timeouts are checked when the awaited event arrives; there is no scheduler. Returns True if it expired."""
        if workflow is None or workflow.state not in SUSPENDED:
            return False
        log = EvaluationLog.load(self.deps.log_path(workflow.run_id))
        if not StateMachine(log).expire_if_due(workflow, event["timestamp"]):
            return False
        if workflow.state == State.HANDOVER:
            log.event("handover", report={"request_summary": workflow.original_request["text"],
                                          "stopped_because": workflow.reason, "suggested_next_step": self.TIMEOUT_NEXT_STEP})
        self.deps.store.save(workflow)
        log.snapshot(self.deps.domain_store.snapshot(workflow.run_id))
        return True