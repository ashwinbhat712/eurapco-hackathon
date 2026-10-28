# Contact update procedure

Scope: holder email, telephone (international format) and full postal address.
Active policy required. No insurer authorization and no second confirmation.

1. Retrieve the current policy with `get_policy`. Never assume fixture defaults.
2. If the policy is not `active`, close: `closed_without_execution` / `inactive_policy`.
3. Determine which contact details the policyholder wants changed, including fields
   named without a value. A bank account or IBAN change is `out_of_scope_iban`; any
   other field is `out_of_scope_other`. When the message mixes a supported change with
   an unsupported one, apply the supported part and say in the answer that the
   unsupported part was not performed.
4. Collect everything that is still missing, in one list: a named field without a new
   value, or an address replacement without street, city, postal code or country.
   Ask **one** question containing all of it (`awaiting_clarification` /
   `information_requested`). Never ask for contact details that are not being changed,
   and never ask twice: if the reply is still incomplete, close with
   `insufficient_information`.
5. Validate representation: the telephone must match `+` followed by 8 to 15 digits,
   and the email must be a real address. Otherwise close with `invalid_input`; the
   tool's own validation confirms the same rule.
6. Compare the new values with the current ones. If every new value is already on
   file, finish with `completed` / `no_change` and call no tool.
7. Otherwise call `update_contact_details` with **only** the fields whose value
   actually changes, and report the before and after values from the tool result.
   Finish with `completed` / `updated`.

An address is always replaced as a whole: the tool requires street, city, postal_code
and country together.
