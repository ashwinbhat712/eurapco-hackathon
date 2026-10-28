"""The live AgentWorker: an LLM that reads requests, fixes format errors and phrases messages."""

import json

from pydantic import BaseModel

from app.contracts import tool_schema
from app.live.extraction import EXTRACTION_BY_AGENT, to_findings
from app.live.prompts import CORRECT_PROMPT, GATHER_PROMPTS, MESSAGE_PROMPT
from app.workflow.ports import Call


class CorrectedArgs(BaseModel):
    args_json: str


def _dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class LlmAgent:
    def __init__(self, agent, client):
        self.agent, self.client = agent, client

    async def _structured(self, payload, output_cls, prompt):
        response = await self.client.a_structured_response(input=_dump(payload), output_cls=output_cls,
                                                           system_prompt=prompt)
        return response.structured_data[0]

    async def gather(self, context, tools):
        payload = context.model_dump(mode="json")
        extraction = await self._structured(payload, EXTRACTION_BY_AGENT[self.agent], GATHER_PROMPTS[self.agent])
        return to_findings(extraction)

    async def propose_write(self, workflow, expected, error, tools):
        """The harness built the call from the rules; the model only repairs a format error the tool reported."""
        if error is None:
            return expected
        payload = {"tool": expected.tool, "input_schema": tool_schema(expected.tool, model_facing=True),
                   "rejected_arguments": expected.args, "error": error}
        corrected = await self._structured(payload, CorrectedArgs, CORRECT_PROMPT)
        return Call(tool=expected.tool, args=json.loads(corrected.args_json))

    async def compose_message(self, workflow, results):
        proposal = workflow.proposal
        payload = {
            "request": workflow.original_request.get("text", ""),
            "clarification": workflow.clarification and {"question": workflow.clarification.question,
                                                         "customer_reply": workflow.clarification.reply},
            "category": workflow.request_category,
            "facts": workflow.facts,
            "decision": workflow.decision.model_dump(mode="json") if workflow.decision else None,
            "proposal": proposal and {"summary": proposal.summary, "status": proposal.status,
                                      "insurer_reason": proposal.decision_reason},
            "tool_results": results,
        }
        response = await self.client.a_invoke(_dump(payload), system_prompt=MESSAGE_PROMPT)
        return response.text.strip()
