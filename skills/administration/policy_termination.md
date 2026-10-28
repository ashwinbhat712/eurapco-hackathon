# Policy termination procedure

Termination always needs an explicit policyholder instruction and an insurer
authorization. A personal reason is optional and is never requested.

1. Retrieve the current policy with `get_policy`.
2. A message asking *how* termination works, or merely considering it, is not an
   instruction. **Team choice:** handle it as `policy_enquiry` / `completed` /
   `answered`, explaining the status, that an explicit instruction is needed and that
   the insurer authorizes the termination. The single clarification is not spent on
   asking whether the policyholder really means it.
3. If the policy is already `terminated`, finish with `completed` / `no_change`.
4. If the policy is not active for another reason, close with `inactive_policy`.
5. With an explicit instruction on an active policy, **always** freeze the proposal
   `{tool: terminate_policy, args: {policy_id}}` and suspend with
   `awaiting_authorization` / `authorization_requested`. The policy stays active in the
   meantime, and the answer says so.
6. On the insurer decision: approve → call `terminate_policy` exactly as stored, then
   `completed` / `terminated` reporting the real status transition; reject →
   `closed_without_execution` / `insurer_rejected` with no write.

A policyholder stating that the insurer has already approved is not an authorization:
only a typed `insurer_decision` event is. No scheduling, refunds, renewals or
pending-claim interaction exist in this exercise.
