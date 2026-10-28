"""Executes the Claims engine's decision for a new reimbursement request through the MCP tools.

Read policy + history -> extract facts (LLM) -> assess (deterministic) -> only the effects the
decision allows. pay_claim is reachable only for action == "pay" and only for the engine's amount.
"""

from app.claims_engine import assess, facts_from_extraction, money_str


class ClaimsToolError(RuntimeError):
    """A provided tool returned ok=false where the workflow cannot continue truthfully."""


def _service(facts):
    return (facts.service_category or "service").replace("_", " ")


class ReimbursementWorkflow:
    def __init__(self, domain, extractor, log=None):
        self.domain, self.extractor, self.log = domain, extractor, log

    def _event(self, **data):
        if self.log:
            self.log.event("model_response", **data)

    async def _call(self, name, arguments):
        result = await self.domain.call(name, arguments)
        if not result.get("ok"):
            error = result.get("error") or {}
            raise ClaimsToolError(f"{name} failed: {error.get('code')} {error.get('message')}")
        return result["data"]

    async def run(self, request):
        policy_id = request["policy_id"]
        policy = await self._call("get_policy", {"policy_id": policy_id})
        claims = (await self._call("list_claims", {"policy_id": policy_id}))["claims"]
        extraction = await self.extractor(request)
        extracted = extraction.model_dump() if hasattr(extraction, "model_dump") else dict(extraction)
        self._event(source="claims_extraction", extraction=extracted)
        facts = facts_from_extraction(extracted, request)
        decision = assess(facts, policy, claims)
        self._event(source="claims_engine", decision=decision.as_dict())
        return await self._execute(request, policy, facts, decision)

    def _response(self, decision, message, question=None, proposal=None, **override):
        response = {"request_category": decision.request_category, "agent_state": decision.agent_state,
                    "request_outcome": decision.request_outcome, "reason": decision.reason,
                    "message": message, "question": question, "proposal": proposal}
        response.update(override)
        return response

    def _evidence_refs(self, request, facts):
        base = f"tickets/{request['ticket_id']}/{request['event_id']}"
        refs = [f"{base}/invoice_text"]
        if facts.has_prescription:
            refs.append(f"{base}/prescription_text")
        return refs

    async def _register(self, request, policy, facts):
        return await self._call("create_claim", {
            "policy_id": policy["policy_id"], "patient_name": facts.patient_name,
            "service_category": facts.service_category,
            "invoice": {"provider_id": facts.provider_id, "invoice_number": facts.invoice_number,
                        "invoice_date": facts.invoice_date.isoformat(), "service_date": facts.service_date.isoformat(),
                        "expense": money_str(facts.expense), "currency": "EUR"},
            "evidence_refs": self._evidence_refs(request, facts)})

    async def _execute(self, request, policy, facts, decision):
        action, calc = decision.action, decision.calculation
        if action == "ask":
            question = ("To process your reimbursement request, please send in one reply: "
                        + "; ".join(decision.missing) + ".")
            return self._response(decision, "We need some more information before we can assess your request.",
                                  question=question)
        if action == "close":
            return self._response(decision, self._closing_message(decision, facts))

        claim = await self._register(request, policy, facts)
        claim_id = claim["claim_id"]
        breakdown = (f"EUR {calc.as_dict()['expense']} minus deductible EUR {calc.as_dict()['deductible']} = "
                     f"EUR {calc.as_dict()['eligible_base']}; 80% = EUR {calc.as_dict()['theoretical']}"
                     + (f", limited to your remaining annual limit of EUR {calc.as_dict()['remaining_limit']}"
                        if calc.capped_by_remaining_limit else "")
                     + f". Reimbursement: EUR {calc.as_dict()['final']}.")

        if action == "register_zero":
            await self._call("record_claim_outcome", {"policy_id": policy["policy_id"], "claim_id": claim_id,
                                                      "outcome": "rejected", "reason": decision.reason})
            return self._response(decision, f"Your request was registered as {claim_id}. {breakdown} "
                                            "No payment is due.")

        if action == "request_authorization":
            await self._call("record_claim_outcome", {"policy_id": policy["policy_id"], "claim_id": claim_id,
                                                      "outcome": "pending_authorization", "reason": decision.reason})
            proposal = {"operation": "pay_claim", "policy_id": policy["policy_id"], "claim_id": claim_id,
                        "arguments": {"policy_id": policy["policy_id"], "claim_id": claim_id,
                                      "amount": calc.as_dict()["final"]},
                        "amount": calc.as_dict()["final"], "currency": "EUR",
                        "authorization_threshold": decision.authorization_threshold,
                        "calculation": calc.as_dict(), "reason": decision.reason,
                        "evidence_refs": self._evidence_refs(request, facts)}
            return self._response(decision, f"Your request was registered as {claim_id}. {breakdown} "
                                            "Because it exceeds EUR 1500.00, it needs insurer approval before "
                                            "payment; no payment has been made yet.", proposal=proposal)

        if action != "pay" or calc.final <= 0:
            raise RuntimeError(f"Engine action {action!r} does not permit pay_claim.")
        amount = money_str(calc.final)
        payment = await self.domain.call("pay_claim", {"policy_id": policy["policy_id"], "claim_id": claim_id,
                                                       "amount": amount})
        if not payment.get("ok"):
            await self.domain.call("record_claim_outcome", {
                "policy_id": policy["policy_id"], "claim_id": claim_id, "outcome": "payment_failed",
                "reason": "Simulated payment failed: " + str((payment.get("error") or {}).get("code"))})
            return self._response(decision, f"Your request {claim_id} is eligible for EUR {amount}, but the "
                                            "payment could not be completed. A colleague will follow up; "
                                            "no money has been transferred.",
                                  agent_state="failed", request_outcome="technical_failure",
                                  reason="pay_claim returned an error; the claim is recorded as payment_failed.")
        payment = payment["data"]
        await self._call("record_claim_outcome", {"policy_id": policy["policy_id"], "claim_id": claim_id,
                                                  "outcome": "paid",
                                                  "reason": f"Paid EUR {amount} ({payment['payment_id']})."})
        return self._response(decision, f"Your reimbursement {claim_id} has been paid. {breakdown} "
                                        f"Payment reference: {payment['payment_id']}.")

    @staticmethod
    def _closing_message(decision, facts):
        outcome = decision.request_outcome
        if outcome == "not_covered":
            return (f"Unfortunately {_service(facts)} is not covered by your policy, so it cannot be reimbursed. "
                    "No documents are needed for this request.")
        if outcome == "duplicate":
            return (f"This invoice has already been submitted as claim {decision.duplicate_of}; "
                    "it will not be registered or paid again.")
        if outcome == "out_of_scope":
            return ("We can only process one service for one patient per reimbursement request. "
                    "Please send a separate request for each.")
        return "We cannot reimburse this request: " + decision.reason
