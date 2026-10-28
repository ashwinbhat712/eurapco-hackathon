# Policy enquiry and scope boundaries

## Policy enquiry (`policy_enquiry`)

A question about the policy is answered from facts, with **no write of any kind** and no
insurer involvement.

1. Call `get_policy`.
2. Answer only what was asked, using the returned facts: `status`, `coverage_period`
   (`start`/`end`), `insured_people`, `covered_services`, `excluded_services`, `contacts`,
   `financial_terms.annual_limit`, `financial_terms.deductible`,
   `financial_terms.remaining_limit`.
3. Quote concrete values and currency, never approximations or remembered defaults.
4. Result: `completed` / `answered`.

If a fact the policyholder asks about is not in the policy data, say so plainly instead of
inventing it; the enquiry is still `answered`.

Example (training ticket T01): "Is physiotherapy covered, and what are my deductible and
remaining annual limit?" → physiotherapy is in `covered_services`, deductible `50.00` EUR,
remaining limit `2896.00` EUR → `policy_enquiry`, `completed` / `answered`.

A question about *how* to terminate, *how* to change coverage or *how* to update contacts
is also an enquiry: explain the procedure from the facts and perform nothing.

## Out of scope

Administration recognises these but never executes them. All of them give
`closed_without_execution` / `out_of_scope`, with no tool write, and no clarification
question — the intent is clear, it is simply not supported.

| Request | Category |
|---|---|
| Change IBAN or bank/payment details | `out_of_scope_iban` |
| Renew, extend or restart the policy; change the coverage period | `out_of_scope_renewal` |
| Anything about a reimbursement, invoice, payment of a claim or claim status | `out_of_scope_claims` |
| Add or remove an insured person, change a name or date of birth, edit policy rules, premiums, underwriting or effective dates | `out_of_scope_other` |

Never perform a claims operation with administration tools, and never approximate an
unsupported operation with a supported one (for example, do not terminate a policy because
a renewal was refused).

## Unclear

If the intent genuinely cannot be identified — no recognisable operation and no question —
use category `unclear`, `closed_without_execution` / `unclear`. Explain what was not
understood and close; do **not** spend the single clarification on an unidentifiable
message.

Ambiguity *within* a recognised operation (a missing address part, an unstated final
coverage value) is not `unclear`: that is the one clarification described in the operation
skills.
