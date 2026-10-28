## Part 3: Claims rules in detail

Checks run in this order. The first match decides the result.

| Step | Check | Source of facts | Result if it fails |
|---|---|---|---|
| 1 | Service type is excluded (dental, cosmetic, medicines) | invoice text → `excluded_services` | `closed_without_execution` / `not_covered`. Do **not** ask for more documents. Approval cannot override this. |
| 2 | Policy is active | `get_policy.status` | `closed_without_execution` / `inactive_policy` |
| 3 | Required evidence is complete. **Invoice:** provider ID, invoice number, invoice date, service date, patient, service description, EUR amount, paid status. **Prescription** (diagnostic test and physiotherapy only): patient and requested service. | `invoice_text`, `prescription_text` | First time: `awaiting_clarification` / `information_requested`, asking for **all** missing items at once. Still missing after the reply: `closed_without_execution` / `insufficient_information`. |
| 4 | Patient is one of `insured_people` | `get_policy` | `closed_without_execution` / `not_insured` |
| 5 | Service date is within `coverage_period`, both first and last day included | `get_policy` | `closed_without_execution` / `outside_coverage_period` |
| 6 | Duplicate: same provider ID + invoice number + invoice-date year, with status paid **or** pending | `list_claims` | `closed_without_execution` / `duplicate`. Report the existing claim reference. Do not create or pay anything. |
| 7 | Calculate the reimbursement:<br>`base = max(0, expense − deductible)`<br>`theoretical = round_half_up(0.80 × base, 0.01)`<br>`final = min(theoretical, remaining_limit)` | `financial_terms` from a **fresh** `get_policy` | `final == 0` → `zero_reimbursement`. Completion versus closing is a team choice; document it. |
| 8 | Approval needed: `final > 1500`. Exactly 1500 is autonomous. | `authorization_thresholds.reimbursement` | `awaiting_authorization` / `authorization_requested`, with a frozen proposal (`claim_id`, `amount`) |
| 9 | Execute: `pay_claim` with exactly the calculated or approved amount | – | `completed` / `paid`. Insurer rejects → `closed_without_execution` / `insurer_rejected`, plus `record_claim_outcome`. |

### Claim lifecycle (team decision; pick one and use it consistently)

- Call `create_claim` after steps 1–6 pass, so a pending record exists for the duplicate check and the approval request.
- Call `record_claim_outcome` on a rejection or zero reimbursement.
- Never call `pay_claim` twice. Same claim and amount returns `created=false`, which is fine; a different amount fails.

### Unit tests (from docs/policy.md, deductible 50)

| Expense | Remaining limit | Expected | Needs approval |
|---|---|---|---|
| 180 | 2896 | 104.00 | No |
| 1925 | 2896 | 1500.00 | No (equal to threshold) |
| 2000 | 2896 | 1560.00 | Yes |
| 2000 | 900 (POL-002) | 900.00 | No |
| 40 | any | 0.00 | → `zero_reimbursement` |

Also add tests for half-up rounding at a `.xx5` boundary, the first and last day of the coverage period, a duplicate in the same year (match) and a different year (no match), and a duplicate whose existing claim is pending.

---

## Part 4: Administration rules in detail

### Contact update (`contact_update`)

| Check | Result |
|---|---|
| Policy not active | `closed_without_execution` / `inactive_policy` |
| Field is not email, phone or address | Out of scope; IBAN goes to `out_of_scope_iban` |
| Address replacement is missing street, city, postal_code or country | Ask once (`information_requested`). Still missing after the reply → `insufficient_information`. |
| Phone is not in international format | `invalid_input`; let the tool's own validation confirm it |
| All new values equal the current ones | `completed` / `no_change` |
| Otherwise | Call `update_contact_details` with **only** the changed fields → `completed` / `updated`. No approval needed. |

### Coverage change (`coverage_change`)

| Check | Result |
|---|---|
| Policy not active | `closed_without_execution` / `inactive_policy` |
| Requested value not in the catalog (limit 3000/5000/10000; deductible 50/100/200/500) | `closed_without_execution` / `invalid_input` |
| A requested value is lower than the current value | `closed_without_execution` / `invalid_input` or out of scope. Not decided by the docs; pick one and document it. |
| Final values equal the current values | `completed` / `no_change` |
| **Final** limit > 5000 **or** final deductible > 200. Exactly 5000 or 200 does not count. | `awaiting_authorization` / `authorization_requested`. Freeze a single proposal covering both fields. |
| Otherwise | `update_coverage` → `completed` / `updated` |
| Insurer rejects | `closed_without_execution` / `insurer_rejected`, with **no** partial write |

A combined change is atomic: if either part fails, nothing is written.

### Termination (`policy_termination`)

| Check | Result |
|---|---|
| Message is a question about *how* to terminate | Reclassify as `policy_enquiry` → `answered` |
| No explicit instruction to terminate | Ask once, or treat as an enquiry |
| Already terminated | `completed` / `no_change` |
| Explicit instruction and active policy | **Always** `awaiting_authorization` / `authorization_requested` |
| Insurer approves | `terminate_policy` → `completed` / `terminated` |
| Insurer rejects | `closed_without_execution` / `insurer_rejected` |

A personal reason for terminating is optional; never ask for it.

### Proposal integrity (parts 1 and 4 together)

When a proposal is sent to the insurer, store it as `{tool, args, hash}`. When the decision arrives, execute exactly that stored call. Never re-ask the model for the arguments, and refuse to run anything that does not match the stored hash.