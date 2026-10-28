// pizzaclaim pitch: file explorer, keyboard nav and scroll reveal.

const FILES = {
  "states.py": [
    ["c", "# The table is the single source of truth."],
    ["c", "# StateMachine.transition() refuses any move not listed here."],
    ["", "_FORWARD = {"],
    ["", "    State.RECEIVED: {State.ROUTED},"],
    ["c", "    # The router never closes a ticket."],
    ["", "    State.ROUTED: {State.GATHERING},"],
    ["", "    State.GATHERING: {State.ROUTED, State.AWAITING_CLARIFICATION,"],
    ["", "                      State.CLOSED, State.EVALUATING, State.COMPLETED},"],
    ["", "    State.AWAITING_CLARIFICATION: {State.GATHERING, State.CLOSED},"],
    ["", "    State.EVALUATING: {State.COMPLETED, State.CLOSED,"],
    ["", "                       State.AWAITING_AUTHORIZATION, State.EXECUTING, State.HANDOVER},"],
    ["", "    State.AWAITING_AUTHORIZATION: {State.EXECUTING, State.CLOSED, State.HANDOVER},"],
    ["", "    State.EXECUTING: {State.COMPLETED, State.CLOSED, State.HANDOVER},"],
    ["", "}"],
    ["c", "# Any non-terminal state may fail; terminal states never move again."],
  ],
  "guard.py": [
    ["k", "async def _once(self, call):"],
    ["", "    digest = CallCheck.digest((call,))"],
    ["", "    entry = last_ledger_entry_with(digest)"],
    ["", "    if entry and entry.status == LedgerStatus.DONE:"],
    ["k", "        return entry.result  # Never repeat a successful write."],
    ["", "    if entry and call.tool == Tool.CREATE_CLAIM:"],
    ["c", "        # Not idempotent: look it up by provider + invoice no + year."],
    ["", "        found = await self._find_created(call)"],
    ["", "        if found is not None:"],
    ["", "            return self._settle(entry, found)"],
    ["", "    if entry is None:"],
    ["", "        entry = LedgerEntry(tool=call.tool, args=call.args, hash=digest)"],
    ["", "        self.workflow.write_ledger.append(entry)"],
    ["k", "        self.save()  # write-ahead: persisted BEFORE the domain call"],
    ["", "    return self._settle(entry, await self.domain.call(call.tool, call.args))"],
  ],
  "guard.py#owned": [
    ["c", "# The model may propose a write. These fields are always ours."],
    ["", "HARNESS_OWNED = (\"policy_id\", \"claim_id\", \"amount\", \"outcome\")"],
    ["", "MODEL_STATES  = frozenset({State.EXECUTING})"],
    ["", "MAX_PER_ERROR = 3  # docs/tools.md"],
    ["", "MAX_PER_TOOL  = 3  # a model hopping between fields still stops"],
    ["", ""],
    ["c", "# A correction may change only the field the error names."],
    ["", "if self._without(proposed.args, path) != self._without(previous.args, path):"],
    ["s", "    return \"The call changes facts or fields other than the one the error names.\""],
  ],
  "duplicates.py": [
    ["c", "# Duplicate key (docs/policy.md):"],
    ["c", "#   provider ID + invoice number + invoice-date year"],
    ["", "BLOCKING_STATUSES = frozenset({"],
    ["", "    ClaimStatus.PAID,"],
    ["", "    ClaimStatus.PENDING_AUTHORIZATION,"],
    ["", "    ClaimStatus.REGISTERED,"],
    ["k", "    ClaimStatus.PAYMENT_FAILED,  # still a duplicate!"],
    ["", "})"],
  ],
  "eligibility.py": [
    ["c", "# Every reason not to reimburse, in rule order. Facts only; nothing inferred."],
    ["", "return CheckResult.collect(("],
    ["", "    (status != ACTIVE,          Outcome.INACTIVE_POLICY, ...),"],
    ["", "    (excluded,                  Outcome.NOT_COVERED, ...),"],
    ["", "    (not insured,               Outcome.NOT_INSURED, ...),"],
    ["", "    (not start <= served <= end, Outcome.OUTSIDE_COVERAGE_PERIOD, ...),"],
    ["k", "    (invoiced < served,         Outcome.INVALID_INPUT, \"Invoice before service.\"),"],
    ["k", "    (currency != \"EUR\",         Outcome.INVALID_INPUT, ...),"],
    ["", "))"],
  ],
};

function esc(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function showFile(name) {
  const lines = FILES[name].map(([cls, text]) => `<span class="l ${cls}">${esc(text)}</span>`);
  document.getElementById("code-pre").innerHTML = lines.join("");
  document.getElementById("code-name").textContent = name.split("#")[0];
  document.querySelectorAll("[data-file]").forEach((b) => b.classList.toggle("on", b.dataset.file === name));
}

document.querySelectorAll("[data-file]").forEach((b) => b.addEventListener("click", () => showFile(b.dataset.file)));
showFile("guard.py");

const KEYS = Object.fromEntries([...document.querySelectorAll("nav a.tab")].map((a) => [a.dataset.key, a.hash]));
document.addEventListener("keydown", (e) => {
  const target = KEYS[e.key.toUpperCase()];
  if (target && !e.metaKey && !e.ctrlKey && !e.altKey) location.hash = target;
});

const io = new IntersectionObserver((entries) => entries.forEach((en) => en.isIntersecting && en.target.classList.add("in")),
  { threshold: 0.12 });
document.querySelectorAll(".reveal").forEach((el) => io.observe(el));
