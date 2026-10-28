"""Claims rules (docs/policy.md) over the shared checks in app/checks: reimbursement and claim enquiry."""

from app.checks.base import ClaimStatus, Same, Service
from app.checks.duplicates import DuplicateFinder
from app.checks.eligibility import EligibilityCheck
from app.checks.money import Money
from app.checks.reimbursement import Reimbursement
from app.contracts import Tool
from app.live.common import ask, close, get_policy, read
from app.workflow.ports import Action, AuthorizationPlan, Call, Decision, InformationCheck
from app.workflow.states import Outcome

COVERED = frozenset({Service.SPECIALIST_CONSULTATION, Service.DIAGNOSTIC_TEST, Service.PHYSIOTHERAPY})
NEEDS_PRESCRIPTION = frozenset({Service.DIAGNOSTIC_TEST, Service.PHYSIOTHERAPY})
CREATE_STEP = 0  # create_claim is always the decision's first call; later calls reference its claim_id.
INVOICE_FIELDS = ("patient_name", "service_description", "provider_id", "invoice_number", "invoice_date",
                  "service_date", "expense", "currency", "invoice_paid")
PRESCRIPTION_FIELDS = ("prescription_patient", "prescription_service")
EVIDENCE_FIELDS = ("invoice_text", "prescription_text")
LABELS = {
    "service_category": "which medical service you want reimbursed",
    "patient_name": "the patient's name on the invoice", "service_description": "the service description",
    "provider_id": "the provider ID", "invoice_number": "the invoice number", "invoice_date": "the invoice date",
    "service_date": "the service date", "expense": "the invoice amount", "currency": "the invoice currency (EUR)",
    "invoice_paid": "proof that the invoice is paid",
    "prescription_patient": "a prescription for the same patient as on the invoice",
    "prescription_service": "a prescription for the same service as on the invoice",
}


def check_reimbursement(workflow):
    facts = workflow.facts
    service = facts.get("service_category")
    if service and service not in COVERED:  # Excluded or unknown service: explain and close, never ask.
        return InformationCheck(close=Outcome.NOT_COVERED,
                                reason=f"{service.replace('_', ' ')} is not covered by the policy; nothing was paid.")
    fields = INVOICE_FIELDS + (PRESCRIPTION_FIELDS if service in NEEDS_PRESCRIPTION else ())
    missing = [*(("service_category",) if not service else ()),
               *(key for key in fields if facts.get(key) in (None, "", False))]
    other_patient = (service in NEEDS_PRESCRIPTION and facts.get("prescription_patient")
                     and not Same.name(facts["prescription_patient"], facts.get("patient_name")))
    other_service = (service in NEEDS_PRESCRIPTION and facts.get("prescription_service")
                     and facts["prescription_service"] != service)
    mismatched = {"prescription_patient": other_patient, "prescription_service": other_service}
    missing += [key for key, wrong in mismatched.items() if wrong and key not in missing]
    return InformationCheck(missing=tuple(missing), question=ask(LABELS, missing) if missing else "")


def check_claim_enquiry(workflow):
    return InformationCheck()


async def decide_claim_enquiry(workflow, reader, agent):
    claim_id = workflow.facts.get("claim_id") or workflow.original_request.get("claim_id")
    tool, args = (Tool.GET_CLAIM, {"claim_id": claim_id}) if claim_id else (Tool.LIST_CLAIMS, {})
    result = await reader.call(tool, {"policy_id": workflow.policy_id, **args})
    message = await agent.compose_message(workflow, [result])
    return Decision(action=Action.ANSWER, outcome=Outcome.ANSWERED, reason=f"Answered from {tool}.", message=message)


def _evidence_refs(workflow):
    reply = getattr(workflow.clarification, "reply", None) or {}
    texts = [key for key in EVIDENCE_FIELDS if workflow.original_request.get(key) or reply.get(key)]
    return [f"{workflow.ticket_id}:{key}" for key in texts or ["text"]]


def _create_call(workflow, facts):
    invoice = {key: facts[key].strip() for key in ("provider_id", "invoice_number", "invoice_date", "service_date")}
    invoice.update(expense=Money.format(Money.parse(facts["expense"])), currency=Money.CURRENCY)
    return Call(tool=Tool.CREATE_CLAIM, args={"policy_id": workflow.policy_id, "patient_name": facts["patient_name"],
                                              "service_category": facts["service_category"], "invoice": invoice,
                                              "evidence_refs": _evidence_refs(workflow)})


def _record(workflow, outcome, reason):
    return Call(tool=Tool.RECORD_CLAIM_OUTCOME, args={"policy_id": workflow.policy_id,
                                                      "claim_id": Call.ref(CREATE_STEP, "claim_id"),
                                                      "outcome": outcome, "reason": reason})


async def decide_reimbursement(workflow, reader, agent):
    facts, policy = workflow.facts, await get_policy(reader, workflow)
    eligibility = EligibilityCheck(policy).check(
        patient_name=facts["patient_name"], service_category=facts["service_category"],
        service_date=facts["service_date"], invoice_date=facts["invoice_date"], expense=facts["expense"],
        currency=facts.get("currency"))
    if not eligibility.ok:
        return close(eligibility.outcome, f"{eligibility.reason} Nothing was paid.")
    listed = await read(reader, Tool.LIST_CLAIMS, {"policy_id": workflow.policy_id})
    duplicate = DuplicateFinder(listed["claims"]).find(provider_id=facts["provider_id"],
                                                        invoice_number=facts["invoice_number"],
                                                        invoice_date=facts["invoice_date"])
    if duplicate.found:
        existing = duplicate.duplicate
        return close(Outcome.DUPLICATE, f"This invoice was already submitted as {existing['claim_id']} "
                     f"(status {existing['status']}); nothing new was created or paid.")
    result = Reimbursement.for_policy(policy, facts["expense"])
    calculation = f"Calculation: {result.explain()}"
    if result.zero:
        return close(Outcome.ZERO_REIMBURSEMENT, f"{calculation} There is nothing to reimburse; nothing was paid.")
    amount, threshold = Money.with_currency(result.final), Money.with_currency(result.authorization_threshold)
    create, claim_ref = _create_call(workflow, facts), Call.ref(CREATE_STEP, "claim_id")
    pay = Call(tool=Tool.PAY_CLAIM, args={"policy_id": workflow.policy_id, "claim_id": claim_ref,
                                          "amount": Money.format(result.final)})
    if not result.needs_authorization:
        return Decision(action=Action.EXECUTE, outcome=Outcome.PAID, reason=calculation, calls=(create, pay))
    pending = _record(workflow, ClaimStatus.PENDING_AUTHORIZATION,
                      f"{amount} exceeds {threshold}; awaiting the insurer.")
    plan = AuthorizationPlan(
        summary=f"Pay {amount} to {facts['patient_name']} on {workflow.policy_id} for "
                f"{facts['service_category']} (invoice {facts['provider_id']}/{facts['invoice_number']}). {calculation}",
        outcome=Outcome.PAID, on_approve=(pay,),
        on_reject=(_record(workflow, ClaimStatus.REJECTED, "Insurer rejected."),))
    return Decision(action=Action.ASK_AUTHORIZATION, outcome=Outcome.PAID,
                    reason=f"{calculation} {amount} exceeds the authorization threshold {threshold}.",
                    calls=(create, pending), authorization=plan)
