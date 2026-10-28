# Coverage change (`coverage_change`)

Request category: `coverage_change`. Only increases of the annual limit and/or the
per-claim deductible are supported. No underwriting, no premium calculation, no
effective-date workflow.

Catalog (confirm against `allowed_values` from `get_policy`):

- annual limit: `3000.00`, `5000.00`, `10000.00`
- deductible: `50.00`, `100.00`, `200.00`, `500.00`

Thresholds (confirm against `authorization_thresholds`): annual limit `5000.00`,
deductible `200.00`.

## Ordered checks — the first failure decides

| # | Check | Result when it fails |
|---|---|---|
| 1 | `status == "active"` | `closed_without_execution` / `inactive_policy` |
| 2 | Intended **final** values are stated (not "raise it a bit") | `awaiting_clarification` / `information_requested`, one question asking for the intended final annual limit and/or deductible. Insufficient reply → `closed_without_execution` / `insufficient_information` |
| 3 | Every requested value is in the catalog | `closed_without_execution` / `invalid_input`, naming the allowed values |
| 4 | No requested value is **lower** than the current value | `closed_without_execution` / `invalid_input` — decreases are not supported (team decision; the category stays `coverage_change`) |
| 5 | At least one final value differs from the current one | all equal → `completed` / `no_change`, **no tool call** |
| 6 | Final annual limit ≤ 5000.00 **and** final deductible ≤ 200.00 | otherwise `awaiting_authorization` / `authorization_requested` with one frozen proposal (see the proposal skill) |
| 7 | Otherwise | call `update_coverage` → `completed` / `updated` |

Compare **final values, not increments**. Exactly `5000.00` and exactly `200.00` are
autonomous; only strictly above the threshold needs the insurer.

## Atomicity

A combined limit + deductible change is **one** operation.

- If either component is out of catalog, a decrease, or otherwise inadmissible, close the
  whole request and write nothing — never apply "the valid half".
- If either final value crosses its threshold, the **whole** change (both fields) goes into
  a single proposal; do not apply the autonomous field early.
- On insurer rejection nothing is written at all: `closed_without_execution` /
  `insurer_rejected`.

## Calling the tool

```json
{"policy_id": "POL-001", "changes": {"annual_limit": "5000.00", "deductible": "200.00"}}
```

Values are strings with two decimals. `changes` carries only the fields that actually
differ from the current policy values; a field already at the requested value is omitted.

## Worked examples (POL-001: limit 3000.00, deductible 50.00)

| Request | Final values | Decision |
|---|---|---|
| T05: limit → 5000, deductible → 200 | 5000.00 / 200.00 | Both equal to thresholds → autonomous. One `update_coverage` call with both fields → `completed` / `updated` |
| T06: limit → 10000, deductible → 500 | 10000.00 / 500.00 | Both above thresholds → one combined proposal → `awaiting_authorization` / `authorization_requested`. On approval, execute the stored call → `completed` / `updated` |
| limit → 10000 only | 10000.00 / 50.00 | Above threshold → proposal with `{"annual_limit": "10000.00"}` only |
| deductible → 100 only | 3000.00 / 100.00 | Below threshold → autonomous `update_coverage` → `updated` |
| limit → 3000 (already 3000) | 3000.00 / 50.00 | No difference → `completed` / `no_change`, no call |
| limit → 4000 | — | Not in catalog → `closed_without_execution` / `invalid_input` |
| deductible → 500, limit → 1000 | — | 1000 not in catalog → whole request `invalid_input`, deductible not applied |

## Reporting

Report the real `before`/`after` values from the tool result and mention that a higher
deductible means the insured contributes more per claim. Never report an intended value as
applied.
