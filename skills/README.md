# Domain skills

Loading support is supplied in app/skills.py. Write your Markdown skills, decide when/how to
load them into the agents, and preserve the documented business behavior. Framework and integration choices belong to your team.

- `claims/reimbursement.md`: reimbursement procedure (service mapping, extraction rules, decision
  order, calculation). Loaded into the Datapizza extraction agent (`app/claims_extraction.py`) and
  appended to the Claims agent prompt. The decisions themselves are enforced in code by
  `app/claims_engine.py`; keep both in sync.
