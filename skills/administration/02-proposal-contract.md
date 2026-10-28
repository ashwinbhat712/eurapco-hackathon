# Proposal integrity (insurer authorization)

Administration asks the insurer in exactly two situations:

- `coverage_change` where the **final** annual limit is above EUR 5000 **or** the **final**
  deductible is above EUR 200 (exactly 5000 or exactly 200 is autonomous);
- every actual `policy_termination` on an active policy.

Nothing else. A contact update never needs authorization.

## The frozen proposal

When authorization is needed, return `agent_state=awaiting_authorization`,
`request_outcome=authorization_requested`, no write performed, and a `proposal` object with
exactly these keys:

```json
{
  "operation": "coverage_change",
  "tool": "update_coverage",
  "args": {"policy_id": "POL-001", "changes": {"annual_limit": "10000.00", "deductible": "500.00"}},
  "policy_id": "POL-001",
  "reason": "Final annual limit 10000.00 EUR exceeds the 5000.00 EUR threshold and final deductible 500.00 EUR exceeds the 200.00 EUR threshold.",
  "evidence_refs": ["request/EV-T06-01/text", "tool/get_policy"]
}
```

Termination uses the same shape:

```json
{
  "operation": "policy_termination",
  "tool": "terminate_policy",
  "args": {"policy_id": "POL-001"},
  "policy_id": "POL-001",
  "reason": "Explicit policyholder instruction to terminate an active policy; termination always requires insurer authorization.",
  "evidence_refs": ["request/EV-T07-01/text", "tool/get_policy"]
}
```

Rules for `args`:

- They are the **complete, literal** tool call to execute if the insurer approves, matching
  the tool input schema, minus `run_id` (the client binds it).
- Money values are strings with two decimals from the catalog: `"3000.00"`, `"5000.00"`,
  `"10000.00"`, `"50.00"`, `"100.00"`, `"200.00"`, `"500.00"`.
- `changes` contains **only** the fields that actually differ from the current policy
  values. A field already equal to the requested value is not written and not proposed.
- `evidence_refs` cite the request text and the `get_policy` reading the decision rests on.

## One proposal, never revised

- **One** proposal per request. A combined limit + deductible change is a single proposal
  covering both fields; never split it into two, and never send one field now and the other
  later.
- Emit the proposal once and stop. Do not call the write tool before the insurer's real
  decision arrives, and do not call it "to check" anything.
- The proposal is frozen the moment it is sent. After a decision arrives, execute exactly
  the stored `tool` and `args`. Never re-derive, re-price, round, widen or narrow them, and
  never substitute a different amount or value, even if the insurer's reason suggests one.
- If a call is requested that does not match the stored proposal, refuse it and report
  `failed` / `handover_required` rather than writing something else.

## After the decision

| Decision | What you do | Result |
|---|---|---|
| Approved | Call the stored `tool` with the stored `args`, once | tool confirms → `completed` / `updated` (coverage) or `completed` / `terminated` (termination) |
| Approved, tool then fails | Do not retry a business rejection; report the real error | `failed` / `technical_failure`, or `handover_required` after three format corrections |
| Rejected | Perform **no** write at all, not even a partial one; explain the insurer's reason | `closed_without_execution` / `insurer_rejected` |

There is no second round: no revised proposal, no repeated approval request, no additional
policyholder confirmation after a decision. An approval never makes an unsupported
operation (a decrease, an out-of-catalog value, an IBAN change) permitted — if the request
was inadmissible, it was closed before any proposal existed.
