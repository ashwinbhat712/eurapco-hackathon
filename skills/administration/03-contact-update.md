# Contact update (`contact_update`)

Request category: `contact_update`. No insurer authorization, ever. No second policyholder
confirmation.

## Ordered checks — the first failure decides

| # | Check | Result when it fails |
|---|---|---|
| 1 | `get_policy().status == "active"` | `closed_without_execution` / `inactive_policy` |
| 2 | Every requested field is email, phone or postal address | IBAN or bank details → category `out_of_scope_iban`, `closed_without_execution` / `out_of_scope`. Any other field (name, date of birth, adding an insured person) → category `out_of_scope_other`, `closed_without_execution` / `out_of_scope` |
| 3 | An address replacement supplies `street`, `city`, `postal_code` **and** `country` | First time: `awaiting_clarification` / `information_requested`, one question naming **all** missing address parts. After an insufficient reply: `closed_without_execution` / `insufficient_information` |
| 4 | A phone number matches international format `+` then 8–15 digits (for example `+393339998888`) | `closed_without_execution` / `invalid_input`. Let the tool's own validation confirm the format when you are unsure: send the value and read the `INVALID_ARGUMENT` response |
| 5 | At least one requested value differs from the current one | all equal → `completed` / `no_change`, **no tool call** |
| 6 | Otherwise | call `update_contact_details` → `completed` / `updated` |

If a request mixes a supported contact field with an unsupported one (for example "change
my phone and my IBAN"), apply the supported change and state plainly that the IBAN part is
out of scope and was not performed.

## Address rule

An address is replaced as a whole, never patched. If the policyholder supplies only part of
a new address, the missing parts are genuinely missing information — the current stored
address must not be used to fill the gaps. Ask once for exactly the missing parts, and for
nothing else: never ask about email or phone that were not part of the request.

Example (training ticket T04, "8 Orchard Road, Turin, Italy", no postal code):
`awaiting_clarification` / `information_requested`, question = "Please confirm the postal
code for your new address at 8 Orchard Road, Turin, Italy." Nothing is written.

## Calling the tool

```json
{"policy_id": "POL-001", "changes": {"phone": "+393339998888"}}
```

- `changes` contains **only** the fields that actually change. A field whose new value
  equals the stored one is left out entirely.
- `address` is sent complete: `{"street": ..., "city": ..., "postal_code": ..., "country": ...}`.
- Unspecified contacts remain unchanged; do not resend them.
- Validation is atomic: if any supplied field is invalid, nothing is written. Do not retry
  by dropping the invalid field — that would change what the policyholder asked for.

## Reporting

Quote the real before and after values from the tool result, for example: "Your telephone
number has been updated from +393331234567 to +393339998888. Your email address and postal
address are unchanged." If the result says `changed=false`, report `completed` / `no_change`
instead of `updated`.
