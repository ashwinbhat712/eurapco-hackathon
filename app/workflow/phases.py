"""Automatic phases between suspensions (architecture C). Each phase ends in exactly one transition."""

from app.workflow.ports import Action
from app.workflow.proposal import Proposal, ProposalStatus
from app.workflow.record import Clarification
from app.workflow.schema import Vocabulary
from app.workflow.states import CLOSING_CATEGORIES, HANDOFF_CATEGORY, MAX_REROUTES, REROUTE_TARGET, Outcome, State


class Phases:
    def __init__(self, session):
        self.session, self.workflow = session, session.workflow
        self.steps = {State.RECEIVED: self.route, State.ROUTED: self.start_gathering,
                      State.GATHERING: self.gather, State.EVALUATING: self.evaluate,
                      State.EXECUTING: self.resume_execution}
        self.actions = {Action.ANSWER: self._answer, Action.NO_CHANGE: self._no_change, Action.CLOSE: self._close,
                        Action.EXECUTE: self._execute, Action.ASK_AUTHORIZATION: self._ask_authorization}
        self.authorization_outcomes = {"approve": self._approve, "reject": self._reject}
        self.gather_rules = ((self._is_reroute, self._reroute), (self._is_closing, self._close_category),
                             (self._closes_early, self._close_early), (self._must_ask, self._ask),
                             (self._still_missing, self._insufficient), (self._complete, self._to_evaluating))

    async def run_until_suspended(self):
        while self.workflow.state in self.steps:
            await self.steps[self.workflow.state]()

    # Transition 2-4: the router is called once per workflow; its category is only a hint.
    async def route(self):
        routing = self.session.deps.router.route(self.workflow.original_request["text"])
        self.workflow.handling_agent, self.workflow.category_hint = routing.agent, routing.category_hint
        self.session.log.route(routing.agent)
        self.session.move(State.ROUTED, reason=f"Routed to {routing.agent}.")

    async def start_gathering(self):
        """The router's hint never closes the ticket; the agent always reads the request first."""
        self.session.move(State.GATHERING)

    # Transitions 5-8.
    async def gather(self):
        findings = await self.session.agent.gather(self.workflow.gather_context(), await self.session.read_tools())
        category = self._own_category(findings.category)
        self.workflow.classify(category)
        if category not in REROUTE_TARGET:
            self.workflow.merge_facts(findings.facts)
            self.workflow.note_other_requests(findings.other_requests)
        check = None if category in REROUTE_TARGET | CLOSING_CATEGORIES else self.session.deps.rules.check_information(self.workflow)
        next(action for matches, action in self.gather_rules if matches(check))(check)

    def _own_category(self, category):
        """A category from the other agent's list means the request belongs there: hand it over (same limits)."""
        if category in Vocabulary.BY_AGENT[self.workflow.handling_agent]:
            return category
        return HANDOFF_CATEGORY[Vocabulary.owner(category)]

    def _is_reroute(self, check):
        return self.workflow.request_category in REROUTE_TARGET

    def _reroute(self, check):
        category = self.workflow.request_category
        if self.workflow.reroutes >= MAX_REROUTES or self.workflow.clarification_asked:
            self.session.move(State.CLOSED, outcome=Outcome.OUT_OF_SCOPE, reason=f"{category} after a reroute or question.")
            return
        target = REROUTE_TARGET[category]
        self.session.move(State.ROUTED, reason=f"{self.workflow.handling_agent} says {category}; rerouting.")
        self.workflow.reset_for_reroute(target)
        self.session.log.route(target)
        self.session.save()

    def _is_closing(self, check):
        return self.workflow.request_category in CLOSING_CATEGORIES

    def _close_category(self, check):
        category = self.workflow.request_category
        self.session.move(State.CLOSED, outcome=CLOSING_CATEGORIES[category], reason=f"Agent: {category}.")

    @staticmethod
    def _closes_early(check):
        return check.close is not None

    def _close_early(self, check):
        self.session.move(State.CLOSED, outcome=check.close, reason=check.reason, message=check.reason)

    def _must_ask(self, check):
        return bool(check.missing) and not self.workflow.clarification_asked

    def _ask(self, check):
        self.workflow.clarification = Clarification(question=check.question, missing=list(check.missing))
        self.session.move(State.AWAITING_CLARIFICATION, reason="Missing: " + ", ".join(check.missing),
                          message=check.question, at=self.session.event_time)
        self.session.log.human_requested("clarification", {"question": check.question})

    @staticmethod
    def _still_missing(check):
        return bool(check.missing)

    def _insufficient(self, check):
        self.session.move(State.CLOSED, outcome=Outcome.INSUFFICIENT_INFORMATION,
                          reason="Still missing after the one clarification: " + ", ".join(check.missing))

    @staticmethod
    def _complete(check):
        return True

    def _to_evaluating(self, check):
        self.session.move(State.EVALUATING)

    # Transitions 10-13. The decision is frozen so a crash never re-runs rules over changed domain state.
    async def evaluate(self):
        if self.workflow.decision is None:
            self.workflow.decision = await self.session.deps.rules.decide(self.workflow, await self.session.read_tools())
            self.session.save()
        await self.actions[self.workflow.decision.action](self.workflow.decision)

    async def _answer(self, decision):
        self.session.move(State.COMPLETED, outcome=decision.outcome or Outcome.ANSWERED, reason=decision.reason,
                          message=decision.message)

    async def _no_change(self, decision):
        self.session.move(State.COMPLETED, outcome=Outcome.NO_CHANGE, reason=decision.reason, message=decision.message)

    async def _close(self, decision):
        self.session.move(State.CLOSED, outcome=decision.outcome, reason=decision.reason, message=decision.message)

    async def _execute(self, decision):
        self.session.move(State.EXECUTING, reason=decision.reason)
        await self.resume_execution()

    async def resume_execution(self):
        """EXECUTING after a crash re-runs the frozen batch; the ledger makes done writes no-ops."""
        if self.workflow.proposal is not None:
            await self._run_approved()
            return
        decision = self.workflow.decision
        results = await self.session.run_proposed(decision.calls)
        if results is not None:
            await self.session.finish(State.COMPLETED, decision.outcome, results, decision.reason)

    async def _ask_authorization(self, decision):
        results = await self.session.run_stored(decision.calls) if decision.calls else []
        if results is None:
            return
        self.workflow.proposal = Proposal.freeze(self.workflow, decision.authorization, results)
        summary = self.workflow.proposal.summary
        self.session.move(State.AWAITING_AUTHORIZATION, reason=decision.reason or summary, message=summary,
                          at=self.session.event_time)
        self.session.log.human_requested("authorization", self.workflow.proposal.model_dump(mode="json"))

    # Transitions 14-15: only stored calls run. The model chooses nothing; it only phrases the result.
    async def decide_authorization(self, decision, reason=None):
        proposal = self.workflow.proposal
        if not proposal.verify():
            self.session.move(State.FAILED, reason="The stored proposal failed its integrity check; nothing executed.")
            return
        await self.authorization_outcomes[decision](reason)

    async def _approve(self, reason):
        self.workflow.proposal.status = ProposalStatus.APPROVED
        self.session.move(State.EXECUTING, reason="Insurer approved the stored proposal.")
        await self._run_approved()

    async def _run_approved(self):
        proposal = self.workflow.proposal
        results = await self.session.run_stored(proposal.on_approve)
        if results is not None:
            proposal.status = ProposalStatus.EXECUTED
            await self.session.finish(State.COMPLETED, proposal.outcome, results, f"Insurer approved: {proposal.summary}")

    async def _reject(self, reason):
        proposal = self.workflow.proposal
        proposal.status, proposal.decision_reason = ProposalStatus.REJECTED, reason
        self.session.save()
        text = f"Insurer rejected: {reason}"
        results = await self.session.run_stored(proposal.rejection_calls(reason))
        if results is not None:
            await self.session.finish(State.CLOSED, Outcome.INSURER_REJECTED, results, text)
