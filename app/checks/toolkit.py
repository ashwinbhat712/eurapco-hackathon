"""Which local tools an agent gets, how it is told to use them, and their datapizza wrapping with logging."""

import json
from enum import StrEnum

from app.checks.tools import CalculateReimbursement, CheckCoverageChange, CheckReimbursementEligibility, \
    FindDuplicateClaim
from app.workflow.states import Agent


class LogEvent(StrEnum):
    """Same kinds as the MCP client logs, so a trace reads the same; local tools are marked local=True."""

    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class LocalToolkit:
    ACCESS = {
        Agent.ADMINISTRATION: (CheckCoverageChange,),
        Agent.CLAIMS: (CalculateReimbursement, CheckReimbursementEligibility, FindDuplicateClaim),
    }
    GUIDANCE = {
        Agent.ADMINISTRATION: (
            "Before any update_coverage call, call check_coverage_change with the requested final values and follow "
            "it: issues mean close without writing, no_change means nothing to write, needs_authorization means ask "
            "the insurer with exactly the returned changes."),
        Agent.CLAIMS: (
            "For a new reimbursement call check_reimbursement_eligibility, find_duplicate_claim and "
            "calculate_reimbursement before create_claim or pay_claim, and follow their results. Never compute "
            "amounts yourself: pay and propose exactly the returned final amount; needs_authorization decides "
            "whether insurer approval is required."),
    }
    ERROR_GUIDANCE = (" If one of these tools returns an error with retryable true, correct the named field and call "
                      "it again; with retryable false, do not write anything based on it.")

    def __init__(self, agent, domain, policy_id, log=None):
        """policy_id comes from the ticket, never from the model: every tool is bound to it here."""
        self.tools = [tool(domain, policy_id) for tool in self.ACCESS[agent]]
        self.log = log

    @classmethod
    def guidance(cls, agent):
        return cls.GUIDANCE[agent] + cls.ERROR_GUIDANCE

    async def invoke(self, tool, arguments):
        """Run one tool on model arguments. Unknown keys (e.g. a policy_id) and nulls are dropped, and logged."""
        known = {key: value for key, value in arguments.items() if key in tool.properties and value is not None}
        ignored = sorted(key for key in arguments if key not in tool.properties)
        self._log(LogEvent.TOOL_CALL, name=tool.name, arguments=known, **({"ignored": ignored} if ignored else {}))
        result = await tool(**known)
        self._log(LogEvent.TOOL_RESULT, name=tool.name, result=result)
        return result

    def datapizza_tools(self):
        from datapizza.tools import Tool

        def wrap(tool):
            async def invoke(**arguments):
                return json.dumps(await self.invoke(tool, arguments), ensure_ascii=False)

            return Tool(func=invoke, name=tool.name, description=tool.description,
                        properties={str(key): spec for key, spec in tool.properties.items()},
                        required=list(tool.required))

        return [wrap(tool) for tool in self.tools]

    def _log(self, kind, **data):
        if self.log:
            self.log.event(kind, **data, local=True)
