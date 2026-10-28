# Coverage change procedure

Scope: annual limit (3000 / 5000 / 10000 EUR) and per-claim deductible
(50 / 100 / 200 / 500 EUR). Increases only. A combined change is atomic.

1. Retrieve the current policy with `get_policy` and read `financial_terms`,
   `allowed_values` and `authorization_thresholds` from it.
2. If the policy is not `active`, close: `closed_without_execution` / `inactive_policy`.
3. Work with the intended **final** values, never with increments. If the
   policyholder asks to change a term without saying the final value, ask once for all
   missing values (`awaiting_clarification` / `information_requested`); if the reply is
   still incomplete, close with `insufficient_information`.
4. Reject, without any write, a value outside the catalog and a value lower than the
   current one: `closed_without_execution` / `invalid_input`, keeping the category
   `coverage_change`. **Team choice:** a requested decrease is treated as invalid input
   rather than as an out-of-scope operation, because the operation that was asked for
   is still a coverage change.
5. If the final values already hold, finish with `completed` / `no_change`.
6. Compare the **final** values with the authorization levels: a final annual limit
   above 5000 **or** a final deductible above 200 needs the insurer. Exactly 5000 or
   exactly 200 is autonomous. Freeze **one** proposal covering both fields, store it as
   `{tool, args, hash}` and suspend with `awaiting_authorization` /
   `authorization_requested`. Do not call `update_coverage` before the real decision.
7. Otherwise call `update_coverage` once with the changed fields and report the actual
   before/after values: `completed` / `updated`.
8. On the insurer decision: approve → execute exactly the stored call, then `updated`;
   reject → `closed_without_execution` / `insurer_rejected` with **no** write at all.
   There is no revision, no different value and no second round.

If any component of a combined change is inadmissible, nothing is written. Remind the
policyholder that a higher deductible means a larger share of each invoice is theirs.
No underwriting, premium or effective-date workflow exists here.
