"""Domain subagents built on datapizza-ai. Tools wrap DomainClient so every call is logged."""

import json

from app.contracts import ACCESS, TOOLS, document, tool_schema, validator
from app.skills import load_skills

WRITE_TOOLS = {"update_contact_details", "update_coverage", "terminate_policy",
               "create_claim", "pay_claim", "record_claim_outcome"}
MAX_STEPS = 12


class SubagentRun:
    """One subagent execution over an open DomainClient; tracks successful writes."""

    def __init__(self, agent, client, domain):
        self.agent, self.client, self.domain = agent, client, domain
        self.successful_writes = []

    def tools(self, policy_id=None):
        from datapizza.tools import Tool

        from app.checks.tools import datapizza_tools

        def make(name):
            schema = tool_schema(name, model_facing=True)

            async def invoke(**arguments):
                result = await self.domain.call(name, arguments)
                if name in WRITE_TOOLS and result.get("ok"):
                    self.successful_writes.append(name)
                return json.dumps(result, ensure_ascii=False)

            return Tool(func=invoke, name=name, description=TOOLS[name][1],
                        properties=schema["properties"], required=schema["required"])

        domain_tools = [make(name) for name in ACCESS[self.agent]]
        if not policy_id:
            return domain_tools
        return domain_tools + datapizza_tools(self.agent, self.domain, policy_id, getattr(self.domain, "log", None))

    def system_prompt(self):
        from agents.administration.agent import system_prompt as admin_prompt
        from agents.claims.agent import system_prompt as claims_prompt
        from app.checks.tools import LOCAL_TOOL_GUIDANCE

        prompt = admin_prompt() if self.agent == "administration" else claims_prompt()
        prompt += "\n\n" + LOCAL_TOOL_GUIDANCE[self.agent]
        for name, body in load_skills(self.agent):
            prompt += f"\n\n## Skill: {name}\n{body}"
        prompt += "\n\nReturn ONLY one JSON object matching this response schema when done or waiting:\n"
        prompt += json.dumps(response_schema())
        return prompt

    async def run(self, request, hint=None):
        from datapizza.agents import Agent

        agent = Agent(name=self.agent, client=self.client, system_prompt=self.system_prompt(),
                      tools=self.tools(request.get("policy_id")), max_steps=MAX_STEPS, terminate_on_text=True)
        task = json.dumps(request, ensure_ascii=False)
        if hint:
            task += "\n\n" + hint
        step = await agent.a_run(task)
        if step is None or step.tools_used or not step.text.strip():
            raise RuntimeError("Subagent turn budget exhausted. Stop and hand over; do not repeat successful writes.")
        final = parse_response(step.text)
        if final["request_category"] not in document("vocabulary.json")["request_categories"][self.agent]:
            raise ValueError("Agent returned a category from the other domain.")
        return final


def response_schema():
    return {k: v for k, v in document("agent-response.schema.json").items() if k != "$schema"}


def parse_response(text):
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("Subagent did not return a JSON response.")
    raw = json.loads(text[start:end + 1])
    # Models sometimes echo schema keys (e.g. "$schema"); keep only declared response fields.
    final = {k: v for k, v in raw.items() if k in response_schema()["properties"]}
    final.setdefault("question", None)
    final.setdefault("proposal", None)
    validator("agent-response.schema.json").validate(final)
    return final
