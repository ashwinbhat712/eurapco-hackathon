"""The live Router: the supervisor's routing decision as a first guess for the harness."""

from app.supervisor import classify
from app.workflow.ports import RouteResult
from app.workflow.schema import Vocabulary


class SupervisorRouter:
    def __init__(self, client):
        self.client = client

    async def route(self, request):
        decision = await classify(request, self.client)
        hint = decision.request_category if decision.request_category in Vocabulary.CATEGORIES else None
        return RouteResult(agent=decision.agent, category_hint=hint)
