# Policy termination (`policy_termination`)

An actual termination **always** requires insurer authorization. There is no autonomous
termination, whatever the policy values are.

## Ordered checks — the first match decides

| # | Check | Result |
|---|---|---|
| 1 | The message asks *how* to terminate, what the notice period is, or what the consequences are | This is an enquiry, not an instruction. Category `policy_enquiry`, `completed` / `answered`. Answer from `get_policy` facts. **No write, no proposal.** |
| 2 | Termination is mentioned without an explicit instruction ("I am thinking about cancelling") | Ask once whether the policyholder instructs you to terminate: `awaiting_clarification` / `information_requested`. Insufficient or non-committal reply → `closed_without_execution` / `insufficient_information`. Treating it as an enquiry and answering is also acceptable, but never both |
| 3 | `status` is already `terminated` (or otherwise not active) | `completed` / `no_change`. Do not call `terminate_policy`, do not ask the insurer |
| 4 | Explicit instruction **and** active policy | **Always** `awaiting_authorization` / `authorization_requested`, with the frozen `terminate_policy` proposal. No write yet |

## After the insurer decides

| Decision | Action | Result |
|---|---|---|
| Approved | Call `terminate_policy` once with the stored `args` (`{"policy_id": "POL-001"}`) | tool confirms `after_status: "terminated"` → `completed` / `terminated` |
| Rejected | No call at all | `closed_without_execution` / `insurer_rejected`, explaining the insurer's reason |

If the tool returns `changed=false` (the policy was already terminated), report
`completed` / `no_change` rather than `terminated`.

## Never do these

- Never ask for a personal reason for terminating; it is optional and may be left empty.
- Never ask for a second confirmation after the policyholder's explicit instruction.
- Never schedule a future termination date, discuss renewal, promise a premium refund, or
  check or settle pending claims — all outside scope. If the policyholder asks for one of
  these alongside the termination, terminate as instructed (through authorization) and say
  plainly that the other part is not handled here.
- Never treat a termination request as a chance to re-negotiate coverage.

## Example (training ticket T07)

"Please terminate my medical policy. This is my instruction to terminate, not a request for
information." — POL-001 is active and the instruction is explicit, so the result is
`policy_termination`, `awaiting_authorization` / `authorization_requested`, with the frozen
proposal `{"tool": "terminate_policy", "args": {"policy_id": "POL-001"}}`. Nothing is
written until the insurer's real decision arrives.
