"""Write guard (plan §12) through the engine: corrections, blocking, harness-owned fields, ledger and recovery."""

from app.workflow.guard import ErrorCode
from app.workflow.ports import Action, Call, Decision
from app.workflow.states import Outcome, State

BAD_PHONE, GOOD_PHONE = "+39 333 111 2222", "+393331112222"  # The customer's format vs the tool's format.


class Propose:
    """Scripted model proposals (callables of (expected, error) -> Call)."""

    @staticmethod
    def phone(value, **extra_changes):
        return lambda expected, error: expected.model_copy(
            update={"args": {**expected.args, "changes": {"phone": value, **extra_changes}}})

    @staticmethod
    def args(**overrides):
        return lambda expected, error: expected.model_copy(update={"args": {**expected.args, **overrides}})

    @staticmethod
    def tool(name, **args):
        return lambda expected, error: Call(tool=name, args=args)


class TestCorrection:
    def test_retryable_invalid_argument_is_corrected_on_the_second_attempt(self, harness):
        harness.phone_update(phone=BAD_PHONE)
        harness.admin.proposals.extend([Propose.args(), Propose.phone(GOOD_PHONE)])
        result = harness.send(harness.request())
        assert (result.workflow.state, result.workflow.outcome) == (State.COMPLETED, Outcome.UPDATED)
        assert harness.domain.count("update_contact_details") == 2
        assert harness.admin.proposed[1][1]["field"] == "changes.phone"  # The error went back to the model.
        assert result.workflow.correction_attempts == {"update_contact_details:changes.phone": 1}

    def test_three_failed_corrections_hand_over_with_a_report(self, harness):
        harness.phone_update(phone=BAD_PHONE)  # The fake keeps repeating the failed call.
        workflow = harness.send(harness.request()).workflow
        assert (workflow.state, workflow.outcome) == (State.HANDOVER, Outcome.HANDOVER_REQUIRED)
        assert harness.domain.count("update_contact_details") == 4  # Original + three corrections.
        report = harness.events("handover")[0]["data"]["report"]
        assert len(report["attempts"]) == 4 and report["stopped_because"].startswith(ErrorCode.INVALID_ARGUMENT)
        assert harness.log()["agent_state"] == "closed_without_execution"

    def test_correction_may_not_change_another_field(self, harness):
        harness.phone_update(phone=BAD_PHONE)
        harness.admin.proposals.extend([Propose.args(), Propose.phone(GOOD_PHONE, email="evil@x.com"),
                                        Propose.phone(GOOD_PHONE)])
        workflow = harness.send(harness.request()).workflow
        assert workflow.state == State.COMPLETED
        assert harness.domain.count("update_contact_details") == 2  # The blocked one never reached the domain.
        assert [e["data"]["error_type"] for e in harness.events("error")] == ["GuardBlocked"]
        assert "evil@x.com" not in str(harness.policy())

    def test_first_proposal_must_equal_the_decision(self, harness):
        harness.phone_update()
        harness.admin.proposals.append(Propose.phone("+391111111111"))
        workflow = harness.send(harness.request()).workflow
        assert workflow.state == State.COMPLETED
        assert harness.domain.count("update_contact_details") == 1  # Only the decided value reached the domain.
        assert GOOD_PHONE in str(harness.policy())

    def test_a_different_write_tool_is_blocked(self, harness):
        harness.phone_update()
        harness.admin.proposals.append(Propose.tool("terminate_policy", policy_id="POL-001"))
        workflow = harness.send(harness.request()).workflow
        assert workflow.state == State.COMPLETED
        assert harness.domain.count("terminate_policy") == 0
        assert harness.policy()["status"] == "active"

    def test_the_model_sees_only_the_decided_tool(self, harness):
        harness.phone_update()
        harness.send(harness.request())
        assert harness.admin.proposed[0][2] == ["update_contact_details"]

    def test_a_model_that_never_matches_is_handed_over(self, harness):
        harness.phone_update()
        harness.admin.proposals.extend([Propose.tool("terminate_policy", policy_id="POL-001")] * 5)
        workflow = harness.send(harness.request()).workflow
        assert workflow.state == State.HANDOVER
        assert harness.domain.count("terminate_policy") == 0


class TestHarnessOwnedFields:
    def test_policy_id_from_the_model_is_replaced(self, harness):
        harness.phone_update()
        harness.admin.proposals.append(Propose.args(policy_id="POL-002"))
        harness.send(harness.request())
        assert harness.domain.calls[-1][1]["policy_id"] == "POL-001"
        assert GOOD_PHONE in str(harness.policy()) and GOOD_PHONE not in str(harness.policy(policy_id="POL-002"))

    def test_run_id_from_the_model_is_stripped(self, harness):
        harness.phone_update()
        harness.admin.proposals.append(Propose.args(run_id="RUN-other"))
        assert harness.send(harness.request()).workflow.state == State.COMPLETED
        assert "run_id" not in harness.domain.calls[-1][1]

    def test_amount_from_the_model_is_replaced(self, harness):
        harness.small_claim()
        harness.claims.proposals.extend([Propose.args(), Propose.args(amount="9999.00")])
        workflow = harness.send(harness.request("Please reimburse my invoice")).workflow
        assert (workflow.state, workflow.outcome) == (State.COMPLETED, Outcome.PAID)
        assert harness.domain.calls[-1] == ("pay_claim", {**harness.domain.calls[-1][1], "amount": "1560.00"})


class TestDomainFailures:
    def test_non_retryable_invalid_argument_hands_over_without_correction(self, harness):
        harness.small_claim(invoice_date="2026-10-01", service_date="2026-10-10")
        workflow = harness.send(harness.request("Please reimburse my invoice")).workflow
        assert workflow.state == State.HANDOVER
        assert harness.domain.count("create_claim") == 1 and harness.domain.count("pay_claim") == 0
        assert len(harness.claims.proposed) == 1

    def test_business_refusal_closes_without_retry(self, harness):
        harness.claim_route()
        pay = Call(tool="pay_claim", args={"policy_id": "POL-001", "claim_id": "CL-NOPE", "amount": "1.00"})
        harness.rules.decisions.append(Decision(action=Action.EXECUTE, outcome=Outcome.PAID, reason="Covered.",
                                                calls=(pay,)))
        workflow = harness.send(harness.request()).workflow
        assert (workflow.state, workflow.outcome) == (State.CLOSED, Outcome.INVALID_INPUT)
        assert "NOT_FOUND" in workflow.reason and harness.domain.count("pay_claim") == 1

    def test_operation_failure_fails_the_workflow(self, harness):
        harness.phone_update()
        harness.domain.raise_before["update_contact_details"] = RuntimeError("connection reset")
        workflow = harness.send(harness.request()).workflow
        assert (workflow.state, workflow.outcome) == (State.FAILED, Outcome.TECHNICAL_FAILURE)
        assert workflow.in_flight_event_id is None

    @staticmethod
    def _operation_failed(effect_applied=False):
        return {"ok": False, "error": {"code": ErrorCode.OPERATION_FAILED, "message": "Payment rail down.",
                                       "retryable": False, "effect_applied": effect_applied}}

    def test_failed_payment_marks_the_record_payment_failed(self, harness):
        harness.small_claim()
        harness.domain.fail_with["pay_claim"] = self._operation_failed()
        workflow = harness.send(harness.request("Please reimburse my invoice")).workflow
        assert workflow.state == State.FAILED
        [claim] = harness.claims_on_policy()
        assert claim["status"] == "payment_failed" and claim["payment"] is None
        assert "Payment rail down." in claim["reason"]

    def test_payment_with_unknown_effect_leaves_the_record_alone(self, harness):
        harness.small_claim()
        harness.domain.fail_with["pay_claim"] = self._operation_failed(effect_applied=None)
        assert harness.send(harness.request("Please reimburse my invoice")).workflow.state == State.FAILED
        assert [claim["status"] for claim in harness.claims_on_policy()] == ["registered"]
        assert harness.domain.count("record_claim_outcome") == 0

    def test_other_failed_writes_record_nothing(self, harness):
        harness.phone_update()
        harness.domain.fail_with["update_contact_details"] = self._operation_failed()
        assert harness.send(harness.request()).workflow.state == State.FAILED
        assert harness.domain.count("record_claim_outcome") == 0


class TestScopedReads:
    def test_gathering_cannot_write(self, harness):
        harness.phone_update()
        engine_tools = []

        class Capturing(type(harness.admin)):
            async def gather(self, context, tools):
                engine_tools.append(await tools.call("update_contact_details",
                                                     {"policy_id": "POL-001", "changes": {"phone": GOOD_PHONE}}))
                engine_tools.append([tool["name"] for tool in tools.model_tools()])
                return await super().gather(context, tools)

        capturing = Capturing()
        capturing.findings, capturing.proposals = harness.admin.findings, harness.admin.proposals
        harness.agents["administration"] = capturing
        harness.send(harness.request())
        blocked, visible = engine_tools
        assert blocked["error"]["code"] == ErrorCode.HARNESS_BLOCKED
        assert "update_contact_details" not in visible and "get_policy" in visible
        assert harness.domain.count("update_contact_details") == 1  # Only the guarded write in EXECUTING.
