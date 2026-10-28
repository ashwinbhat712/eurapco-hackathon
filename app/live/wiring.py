"""Builds the WorkflowEngine with the live router, agents and rules."""

import os

from app.live.agent import LlmAgent
from app.live.router import SupervisorRouter
from app.live.rules import LiveRules
from app.workflow.engine import HarnessDeps, WorkflowEngine
from app.workflow.states import Agent

DEFAULT_TEAM_ID = "TEAM-1"


def build_engine(client, fresh=False):
    agents = {agent: LlmAgent(agent, client) for agent in Agent}
    deps = HarnessDeps(router=SupervisorRouter(client), agents=agents, rules=LiveRules(agents),
                       team_id=os.environ.get("TEAM_ID", DEFAULT_TEAM_ID), fresh=fresh)
    return WorkflowEngine(deps)
