"""Fixed customer messages for endings no model phrases. They never echo request text, so a harmful or
injected message is not repeated back."""

from app.workflow.states import CLOSING_CATEGORIES, Outcome

CATEGORY_MESSAGES = {
    "unclear": "We could not tell what you would like us to do, so we have closed this request. "
               "Please send a new message that describes the one change or claim you need.",
    "out_of_scope_iban": "We cannot change bank account (IBAN) details through this service, "
                         "so we have closed this request.",
    "out_of_scope_renewal": "Policy renewals are not handled through this service, so we have closed this request.",
    "out_of_scope_claim_integration": "This kind of claim request is not handled through this service, "
                                      "so we have closed it.",
    "out_of_scope_other": "This request is outside what we can handle through this service, "
                          "so we have closed it.",
}

OUTCOME_MESSAGES = {
    Outcome.OUT_OF_SCOPE: "This request is outside what we can handle through this service, so we have closed it.",
    Outcome.UNCLEAR: CATEGORY_MESSAGES["unclear"],
    Outcome.INSUFFICIENT_INFORMATION: "We did not receive the information we need, so we have closed this request. "
                                      "Nothing was changed.",
    Outcome.NOT_COVERED: "Your policy does not cover this, so nothing was changed or paid.",
    Outcome.INACTIVE_POLICY: "Your policy is not active, so nothing was changed or paid.",
    Outcome.OUTSIDE_COVERAGE_PERIOD: "The date is outside your coverage period, so nothing was changed or paid.",
    Outcome.NOT_INSURED: "The patient is not insured on this policy, so nothing was paid.",
    Outcome.DUPLICATE: "This invoice has already been submitted, so nothing new was created or paid.",
    Outcome.INVALID_INPUT: "The request could not be carried out as submitted, so nothing was changed.",
    Outcome.INSURER_REJECTED: "The insurer did not approve this request, so nothing was changed.",
    Outcome.ZERO_REIMBURSEMENT: "After the deductible there is nothing to reimburse, so nothing was paid.",
    Outcome.HANDOVER_REQUIRED: "Your request has been passed to a colleague who will continue it.",
    Outcome.TECHNICAL_FAILURE: "A technical problem stopped us from completing your request. "
                               "A colleague will look into it.",
}

UNHANDLED_NOTE = "Your message also asked for something else, which we did not handle; please send it separately."


def final_message(workflow, outcome, message):
    """The message a terminal state ends with: the given text, else a fixed one; plus a note on unhandled parts."""
    category = workflow.request_category
    own = CATEGORY_MESSAGES.get(category) if CLOSING_CATEGORIES.get(category) == outcome else None
    text = message or own or OUTCOME_MESSAGES.get(outcome, "")
    return " ".join(part for part in (text, UNHANDLED_NOTE if workflow.other_requests else "") if part)
