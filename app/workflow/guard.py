"""Write guard (plan §12): state gate, decision match, write-ahead ledger, bounded format corrections."""

import copy
from enum import StrEnum
from typing import Any

from pydantic import Field

from app.contracts import Tool
from app.workflow.ports import Call
from app.workflow.proposal import WRITE_TOOLS, CallCheck
from app.workflow.record import LedgerEntry, LedgerStatus
from app.workflow.schema import ValueModel
from app.workflow.states import State


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    NOT_FOUND = "NOT_FOUND"
    REFERENCE_MISMATCH = "REFERENCE_MISMATCH"
    INVALID_STATE = "INVALID_STATE"
    OPERATION_FAILED = "OPERATION_FAILED"
    HARNESS_BLOCKED = "HARNESS_BLOCKED"


class Failure(StrEnum):
    BUSINESS = "business"  # Close, no retry.
    HANDOVER = "handover"  # A human continues.
    TECHNICAL = "technical"  # Failed.


class WriteResult(ValueModel):
    ok: bool
    result: dict[str, Any]
    failure: Failure | None = None
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class WriteGuard:
    MAX_PER_ERROR = 3  # docs/tools.md: at most three correction attempts for the same error.
    MAX_PER_TOOL = 3  # Plan §12.5: a model hopping between fields still stops at three.
    HARNESS_OWNED = ("policy_id", "claim_id", "amount", "outcome")  # Always the harness's values.
    HARNESS_STATES = frozenset({State.EVALUATING, State.EXECUTING, State.AWAITING_AUTHORIZATION})
    MODEL_STATES = frozenset({State.EXECUTING})
    BLOCKED_ERROR_TYPE = "GuardBlocked"
    RECORD_FIELD = "record"
    FAILURE_BY_CODE = {
        ErrorCode.INVALID_ARGUMENT: Failure.HANDOVER,  # Only non-retryable ones get here (plan §8 #18).
        ErrorCode.NOT_FOUND: Failure.BUSINESS,
        ErrorCode.REFERENCE_MISMATCH: Failure.BUSINESS,
        ErrorCode.INVALID_STATE: Failure.BUSINESS,
        ErrorCode.OPERATION_FAILED: Failure.TECHNICAL,
        ErrorCode.HARNESS_BLOCKED: Failure.TECHNICAL,
    }

    def __init__(self, workflow, domain, log, save):
        self.workflow, self.domain, self.log, self.save = workflow, domain, log, save

    async def run_stored(self, call):
        """A harness-built call (proposal branch or authorization prep). No model, no correction."""
        refused = self._refused(call, self.HARNESS_STATES)
        if refused:
            return refused
        result = await self._once(call)
        return self._finish(result, [self._attempt(call, result)])

    async def run_proposed(self, expected, agent, tools):
        """The model proposes the call named in the Decision; format errors go back to it (max 3)."""
        refused = self._refused(expected, self.MODEL_STATES)
        if refused:
            return refused
        attempts, previous, error = [], expected, None
        while True:
            proposed = self._enforce(await agent.propose_write(self.workflow, previous, error, tools), expected)
            mismatch = self._mismatch(proposed, previous, error)
            if mismatch:  # Keep the previous call and error field, so the model can still fix that field.
                result = self._block(proposed, mismatch, (error or {}).get("field"))
            else:
                result, previous = await self._once(proposed), proposed
            attempts.append(self._attempt(proposed, result))
            if result["ok"] or not self._correctable(result["error"]):
                return self._finish(result, attempts)
            if self._exhausted(proposed.tool, result["error"]):
                return WriteResult(ok=False, result=result, failure=Failure.HANDOVER, attempts=attempts)
            error = result["error"]

    def _refused(self, call, states):
        """Writes outside the allowed states, or on any policy but the ticket's own, never reach the domain."""
        checks = ((self.workflow.state not in states or call.tool not in WRITE_TOOLS,
                   f"{call.tool} is not an allowed write in {self.workflow.state}."),
                  (call.args.get("policy_id") != self.workflow.policy_id, "A write may only act on the ticket's own policy."))
        why = next((text for failed, text in checks if failed), None)
        return self._blocked_result(call, why) if why else None

    def _finish(self, result, attempts):
        if result["ok"]:
            return WriteResult(ok=True, result=result, attempts=attempts)
        failure = self.FAILURE_BY_CODE.get(result["error"]["code"], Failure.TECHNICAL)
        return WriteResult(ok=False, result=result, failure=failure, attempts=attempts)

    def _enforce(self, proposed, expected):
        args = {key: value for key, value in proposed.args.items() if key != "run_id"}
        args.update({key: expected.args[key] for key in self.HARNESS_OWNED if key in expected.args})
        return Call(tool=proposed.tool, args=args)

    def _mismatch(self, proposed, previous, error):
        """A correction may change only the field the error names; the first call must equal the Decision."""
        if proposed.tool != previous.tool:
            return f"Expected {previous.tool}, got {proposed.tool}."
        path = (error or {}).get("field")
        if path is None and error is not None:
            changed = any(key not in previous.args or previous.args[key] != value for key, value in proposed.args.items())
            return "Only removing an undeclared field is allowed here." if changed else None
        if self._without(proposed.args, path) != self._without(previous.args, path):
            return "The call changes facts or fields other than the one the error names."
        return None

    @staticmethod
    def _without(args, path):
        stripped = copy.deepcopy(args)
        if not path:
            return stripped
        *parents, leaf = path.split(".")
        node = stripped
        for key in parents:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(leaf, None)
        return stripped

    @staticmethod
    def _correctable(error):
        return error["code"] == ErrorCode.HARNESS_BLOCKED or (
            error["code"] == ErrorCode.INVALID_ARGUMENT and error.get("retryable") is True)

    def _exhausted(self, tool, error):
        key = f"{tool}:{error.get('field', self.RECORD_FIELD)}"
        counts = self.workflow.correction_attempts
        per_tool = sum(count for name, count in counts.items() if name.startswith(f"{tool}:"))
        if counts.get(key, 0) >= self.MAX_PER_ERROR or per_tool >= self.MAX_PER_TOOL:
            return True
        counts[key] = counts.get(key, 0) + 1
        self.save()
        return False

    async def _once(self, call):
        digest = CallCheck.digest((call,))
        entry = next((e for e in reversed(self.workflow.write_ledger)
                      if e.hash == digest and e.status != LedgerStatus.FAILED), None)
        if entry and entry.status == LedgerStatus.DONE:
            return entry.result  # Never repeat a successful write.
        if entry and call.tool == Tool.CREATE_CLAIM:  # Not idempotent: a new record ID each time.
            found = await self._find_created(call)
            if found is not None:
                return self._settle(entry, found)
        if entry is None:
            entry = LedgerEntry(tool=call.tool, args=call.args, hash=digest)
            self.workflow.write_ledger.append(entry)
            self.save()
        return self._settle(entry, await self.domain.call(call.tool, call.args))

    def _settle(self, entry, result):
        entry.status, entry.result = LedgerStatus.DONE if result["ok"] else LedgerStatus.FAILED, result
        self.save()
        return result

    async def _find_created(self, call):
        """Duplicate key from docs/policy.md: provider ID + invoice number + invoice-date year."""
        invoice = call.args["invoice"]
        key = (invoice["provider_id"], invoice["invoice_number"], invoice["invoice_date"][:4])
        listing = await self.domain.call(Tool.LIST_CLAIMS, {"policy_id": call.args["policy_id"]})
        claims = listing["data"]["claims"] if listing["ok"] else []
        match = next((claim for claim in claims if (claim["invoice"]["provider_id"], claim["invoice"]["invoice_number"],
                                                    claim["invoice"]["invoice_date"][:4]) == key), None)
        return {"ok": True, "data": match} if match else None

    def _block(self, call, why, field=None):
        self.log.event("error", error_type=self.BLOCKED_ERROR_TYPE, tool=call.tool, arguments=call.args, message=why)
        error = {"code": ErrorCode.HARNESS_BLOCKED, "message": why, "retryable": True, "effect_applied": False}
        return {"ok": False, "error": {**error, **({"field": field} if field else {})}}

    def _blocked_result(self, call, why):
        result = self._block(call, why)
        result["error"]["retryable"] = False
        return WriteResult(ok=False, result=result, failure=Failure.TECHNICAL, attempts=[self._attempt(call, result)])

    @staticmethod
    def _attempt(call, result):
        return {"tool": call.tool, "args": call.args, "result": result}


class ScopedTools:
    """What the model may see and call in a phase. Writes are never executed here; the guard does that."""

    def __init__(self, domain, log, names):
        self.domain, self.log, self.names = domain, log, frozenset(names)

    def model_tools(self):
        return [tool for tool in self.domain.model_tools() if tool["name"] in self.names]

    async def call(self, name, arguments):
        if name not in self.names or name in WRITE_TOOLS:
            why = f"{name} is not callable in this phase; writes are executed by the harness."
            self.log.event("error", error_type=WriteGuard.BLOCKED_ERROR_TYPE, tool=name, arguments=arguments, message=why)
            return {"ok": False, "error": {"code": ErrorCode.HARNESS_BLOCKED, "message": why, "retryable": False,
                                           "effect_applied": False}}
        return await self.domain.call(name, arguments)
