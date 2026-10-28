"""One event handling: the workflow, its log, open domain clients and guarded batches of writes."""

from contextlib import AsyncExitStack

from app.contracts import ACCESS
from app.workflow.guard import Failure, ScopedTools, WriteGuard
from app.workflow.machine import StateMachine
from app.workflow.proposal import WRITE_TOOLS
from app.workflow.states import TERMINAL, Outcome, State


class Session:
    BUSINESS_OUTCOME = Outcome.INVALID_INPUT  # NOT_FOUND / REFERENCE_MISMATCH / INVALID_STATE (team decision).
    HANDOVER_NEXT_STEP = "Correct the call format by hand and execute it once; never repeat earlier successful writes."

    def __init__(self, deps, workflow, log, event_time):
        self.deps, self.workflow, self.log, self.event_time = deps, workflow, log, event_time
        self.machine = StateMachine(log)
        self._domains, self._stack = {}, AsyncExitStack()
        self._on_failure = {Failure.BUSINESS: self._close_business, Failure.HANDOVER: self._handover_after_write,
                            Failure.TECHNICAL: self._fail_after_write}

    async def __aenter__(self):
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)

    @property
    def agent(self):
        return self.deps.agents[self.workflow.handling_agent]

    def save(self):
        self.deps.store.save(self.workflow)

    def move(self, target, *, reason="", at=None, **details):
        if target in TERMINAL and self.workflow.notes:
            reason = " ".join([reason, *self.workflow.notes]).strip()
        self.machine.transition(self.workflow, target, reason=reason, at=at, **details)
        self.save()

    async def domain(self):
        """The raw client for the current agent. Only the guard writes through it."""
        name = self.workflow.handling_agent
        if name not in self._domains:
            client = self.deps.open_domain(self.workflow.run_id, name, self.log)
            self._domains[name] = await self._stack.enter_async_context(client)
        return self._domains[name]

    async def read_tools(self):
        names = [tool for tool in ACCESS[self.workflow.handling_agent] if tool not in WRITE_TOOLS]
        return ScopedTools(await self.domain(), self.log, names)

    async def _guard(self):
        return WriteGuard(self.workflow, await self.domain(), self.log, self.save)

    async def run_stored(self, calls):
        """Harness-built calls in order; references bind to earlier results. None if the workflow was closed."""
        guard, results = await self._guard(), []
        for call in calls:
            bound = call.bind(results)
            written = await guard.run_stored(bound)
            if not written.ok:
                self._on_failure[written.failure](bound, written)
                return None
            results.append(written.result)
        return results

    async def run_proposed(self, calls):
        """Each Decision call is proposed by the model with only that tool in view, then guard-checked."""
        guard, results = await self._guard(), []
        for call in calls:
            bound = call.bind(results)
            tools = ScopedTools(await self.domain(), self.log, [bound.tool])
            written = await guard.run_proposed(bound, self.agent, tools)
            if not written.ok:
                self._on_failure[written.failure](bound, written)
                return None
            results.append(written.result)
        return results

    async def finish(self, target, outcome, results, reason):
        """Final message from the ACTUAL results. A message failure must not undo a truthful completion."""
        try:
            message = await self.agent.compose_message(self.workflow, results)
        except Exception as exc:
            self.log.event("error", error_type=type(exc).__name__, message="Message composition failed; template used.")
            message = f"{reason} Outcome: {outcome}."
        self.move(target, outcome=outcome, reason=reason, message=message)

    def handover(self, reason, report):
        self.log.event("handover", report=report)
        self.move(State.HANDOVER, reason=reason)

    def fail(self, exc):
        """Transition 21. Effects already applied stay; the snapshot shows them."""
        reason = f"{type(exc).__name__} in {self.workflow.state}."
        try:
            self.log.event("error", error_type=type(exc).__name__, message=str(exc)[:500])
            self.move(State.FAILED, reason=reason)
        except Exception:  # The logger already saved a failed record; keep the workflow file consistent.
            StateMachine().transition(self.workflow, State.FAILED, reason=reason)
            self.save()

    def _close_business(self, call, written):
        error = written.result["error"]
        self.move(State.CLOSED, outcome=self.BUSINESS_OUTCOME, reason=f"{call.tool} refused: {error['code']}: {error['message']}")

    def _handover_after_write(self, call, written):
        error = written.result["error"]
        self.handover(f"{call.tool} could not be completed.", {
            "request_summary": self.workflow.original_request["text"],
            "request_category": self.workflow.request_category,
            "tried": call.model_dump(mode="json"),
            "attempts": written.attempts,
            "stopped_because": f"{error['code']}: {error['message']}",
            "suggested_next_step": self.HANDOVER_NEXT_STEP,
        })

    def _fail_after_write(self, call, written):
        error = written.result["error"]
        self.move(State.FAILED, reason=f"{call.tool} failed: {error['code']}: {error['message']}")
