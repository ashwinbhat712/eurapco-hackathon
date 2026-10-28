"""Shared helpers for the live rules: reads and fixed decisions."""

from app.checks.base import PolicyStatus
from app.workflow.ports import Action, Decision
from app.workflow.states import Outcome

__all__ = ["PolicyStatus", "ReadFailed", "ask", "close", "get_policy", "inactive_policy", "no_change", "read"]


class ReadFailed(RuntimeError):
    """A read the rules depend on failed; the workflow fails rather than deciding on guesses."""


async def read(reader, tool, args):
    result = await reader.call(tool, args)
    if not result.get("ok"):
        raise ReadFailed(f"{tool} failed: {result.get('error', {}).get('code')}")
    return result["data"]


async def get_policy(reader, workflow):
    return await read(reader, "get_policy", {"policy_id": workflow.policy_id})


def close(outcome, reason, message=""):
    return Decision(action=Action.CLOSE, outcome=outcome, reason=reason, message=message or reason)


def no_change(reason):
    return Decision(action=Action.NO_CHANGE, outcome=Outcome.NO_CHANGE, reason=reason, message=reason)


def inactive_policy(policy):
    return close(Outcome.INACTIVE_POLICY, f"The policy is {policy['status']}, so nothing was changed or paid.")


def ask(labels, missing):
    """One question that names every missing item."""
    return "To continue, please send: " + "; ".join(labels[item] for item in missing) + "."
