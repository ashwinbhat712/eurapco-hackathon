"""LiveRules: deterministic minimum-information checks and decisions per request category."""

from app.live import admin_rules, claims_rules

CHECKS = {
    "policy_enquiry": admin_rules.check_nothing,
    "contact_update": admin_rules.check_contact,
    "coverage_change": admin_rules.check_coverage,
    "policy_termination": admin_rules.check_termination,
    "claim_enquiry": claims_rules.check_claim_enquiry,
    "reimbursement_request": claims_rules.check_reimbursement,
}

DECISIONS = {
    "policy_enquiry": admin_rules.decide_policy_enquiry,
    "contact_update": admin_rules.decide_contact_update,
    "coverage_change": admin_rules.decide_coverage_change,
    "policy_termination": admin_rules.decide_termination,
    "claim_enquiry": claims_rules.decide_claim_enquiry,
    "reimbursement_request": claims_rules.decide_reimbursement,
}


class LiveRules:
    def __init__(self, agents):
        self.agents = agents  # Enquiry answers are phrased by the handling agent from the tool results.

    def check_information(self, workflow):
        return CHECKS[workflow.request_category](workflow)

    async def decide(self, workflow, reader):
        agent = self.agents[workflow.handling_agent]
        return await DECISIONS[workflow.request_category](workflow, reader, agent)
