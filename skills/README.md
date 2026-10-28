# Domain skills

Loading support is supplied in app/skills.py. Write your Markdown skills, decide when/how to
load them into the agents, and preserve the documented business behavior. Framework and integration choices belong to your team.

## administration/

Every `*.md` file in a domain folder is appended to that agent's system prompt in
filename order, so the files are numbered from general to specific:

| File | Covers |
|---|---|
| `01-core-procedure.md` | Facts before action, check ordering, the single clarification, the category/state/outcome split, truthful effect reporting, tool-error correction, authority |
| `02-proposal-contract.md` | When the insurer is asked, and the frozen proposal payload `{operation, tool, args, policy_id, reason, evidence_refs}` |
| `03-contact-update.md` | `contact_update` decision table |
| `04-coverage-change.md` | `coverage_change` decision table, thresholds and atomicity |
| `05-policy-termination.md` | `policy_termination` decision table |
| `06-enquiry-and-scope.md` | `policy_enquiry` answering plus the out-of-scope and `unclear` map |

Two points the documentation leaves to the team are decided here:

- A request to **lower** the annual limit or the deductible stays in category
  `coverage_change` and closes as `closed_without_execution` / `invalid_input`.
- The `proposal` object has the fixed shape documented in `02-proposal-contract.md`, so the
  harness can store and hash it and replay exactly that call on approval. Storing, hashing
  and resuming after the insurer's decision are harness responsibilities, not skill text.

- `claims/reimbursement.md`: reimbursement procedure (service mapping, extraction rules, decision
  order, calculation). Loaded into the Datapizza extraction agent (`app/claims_extraction.py`) and
  appended to the Claims agent prompt. The decisions themselves are enforced in code by
  `app/claims_engine.py`; keep both in sync.
