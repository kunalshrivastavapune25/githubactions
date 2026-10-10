import datetime as dt
import os
from unittest import mock

from support import CS_ID, STACK_ID, FakeClient, PipelineTestCase, stack

import common as c
import execute
import verify_approval

UTC = dt.timezone.utc


def event(i, logical, status, rtype="AWS::SQS::Queue", reason="", physical=None):
    return {"EventId": f"e{i}", "StackId": STACK_ID, "LogicalResourceId": logical, "ResourceType": rtype,
            "ResourceStatus": status, "ResourceStatusReason": reason, "PhysicalResourceId": physical or logical,
            "Timestamp": dt.datetime.now(UTC) + dt.timedelta(seconds=i)}


class Execute(PipelineTestCase):
    env = {"CHANGE_SET_ARN": CS_ID, "CHANGE_SET_NAME": "gha-123-1", "APPROVER": "lead-engineer"}

    def make_cfn(self, final="UPDATE_COMPLETE", events=None, cs_status=("CREATE_COMPLETE", "AVAILABLE"), drifted=False,
                 statuses=None):
        statuses = list(statuses or ["UPDATE_COMPLETE", "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", final])
        timeline = events or [event(1, "demo-stack", "UPDATE_IN_PROGRESS", "AWS::CloudFormation::Stack"),
                              event(2, "Queue", "UPDATE_IN_PROGRESS"), event(3, "Queue", "UPDATE_COMPLETE"),
                              event(4, "demo-stack", final, "AWS::CloudFormation::Stack")]
        calls = {"n": 0}

        def stack_events(StackName, **_):
            calls["n"] += 1
            visible = timeline[: min(len(timeline), calls["n"] * 2)]
            return {"StackEvents": list(reversed(visible))}

        def describe_stacks(StackName):
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            extra = {"Outputs": [{"OutputKey": "QueueUrl", "OutputValue": "https://sqs/app"}]} if status == final else {}
            extra["LastOperations"] = [{"OperationType": "UPDATE_STACK", "OperationId": "op-1"}]
            return {"Stacks": [stack(status, **extra)]}

        drift_rows = [{"LogicalResourceId": "Queue", "StackResourceDriftStatus": "MODIFIED"}] if drifted else []
        self.clients["cloudformation"] = FakeClient(
            describe_change_set={"ChangeSetId": CS_ID, "StackId": STACK_ID, "Status": cs_status[0],
                                 "ExecutionStatus": cs_status[1], "CreationTime": dt.datetime.now(UTC) - dt.timedelta(minutes=5)},
            describe_stacks=describe_stacks,
            list_stack_resources={"StackResourceSummaries": []},
            detect_stack_drift={"StackDriftDetectionId": "d1"},
            describe_stack_drift_detection_status={"DetectionStatus": "DETECTION_COMPLETE",
                                                   "StackDriftStatus": "DRIFTED" if drifted else "IN_SYNC"},
            describe_stack_resource_drifts={"StackResourceDrifts": drift_rows},
            execute_change_set={},
            describe_stack_events=stack_events,
            describe_events={"OperationEvents": [{"EventType": "PROVISIONING_ERROR", "LogicalResourceId": "Queue",
                                                  "ResourceStatusReason": "Resource handler returned message: Invalid value"}]},
        )
        return self.clients["cloudformation"]

    def test_successful_execution(self):
        cfn = self.make_cfn()
        self.assertEqual(self.run_script(execute), 0)
        self.assertEqual(cfn.called("execute_change_set")[0],
                         {"ChangeSetName": CS_ID, "ClientRequestToken": "gha-123-1-exec"})
        self.assertEqual(len(cfn.called("detect_stack_drift")), 1)        # re-checked before executing
        result = c.read_json("reports/execution-result.json")
        self.assertTrue(result["success"])
        self.assertEqual(result["final_stack_status"], "UPDATE_COMPLETE")
        self.assertEqual(result["outputs"][0]["key"], "QueueUrl")
        out = self.outputs()
        self.assertEqual((out["success"], out["output_QueueUrl"]), ("true", "https://sqs/app"))
        self.assertIn("stackinfo?stackId=", out["stack_console_url"])

    def test_failed_execution_finds_the_root_cause(self):
        failing = [event(1, "demo-stack", "UPDATE_IN_PROGRESS", "AWS::CloudFormation::Stack"),
                   event(2, "Queue", "UPDATE_FAILED", reason="Resource handler returned message: Invalid value for VisibilityTimeout"),
                   event(3, "Logs", "UPDATE_FAILED", "AWS::Logs::LogGroup", reason="Resource update cancelled"),
                   event(4, "demo-stack", "UPDATE_ROLLBACK_COMPLETE", "AWS::CloudFormation::Stack")]
        cfn = self.make_cfn(final="UPDATE_ROLLBACK_COMPLETE", events=failing)
        self.assertEqual(self.run_script(execute), 1)
        result = c.read_json("reports/execution-result.json")
        self.assertFalse(result["success"])
        self.assertEqual(result["root_cause"]["logical_id"], "Queue")
        self.assertEqual(len(result["failures"]), 2)
        self.assertEqual(cfn.called("describe_events")[0], {"OperationId": "op-1", "Filters": {"FailedEvents": True}})
        self.assertIn("Root cause", self.summary())

    def test_obsolete_change_set_is_refused(self):
        cfn = self.make_cfn(cs_status=("CREATE_COMPLETE", "OBSOLETE"))
        self.assertEqual(self.run_script(execute), 1)
        self.assertEqual(cfn.called("execute_change_set"), [])
        self.assertIn("OBSOLETE", c.read_json("reports/execution-result.json")["error"])

    def test_new_drift_after_approval_stops_execution(self):
        cfn = self.make_cfn(drifted=True)
        self.assertEqual(self.run_script(execute), 1)
        self.assertEqual(cfn.called("execute_change_set"), [])
        self.assertIn("New drift appeared", c.read_json("reports/execution-result.json")["error"])

    def test_change_set_already_executed_is_reported_not_re_run(self):
        cfn = self.make_cfn(cs_status=("CREATE_COMPLETE", "EXECUTE_COMPLETE"), statuses=["UPDATE_COMPLETE"])
        self.assertEqual(self.run_script(execute), 0)
        self.assertEqual(cfn.called("execute_change_set"), [])
        self.assertTrue(c.read_json("reports/execution-result.json")["already_executed"])


class ApprovalCheck(PipelineTestCase):
    env = {"APPROVAL_ENVIRONMENT": "cfn-change-approval", "GITHUB_TRIGGERING_ACTOR": "kunal", "GH_TOKEN": "t"}

    def run_with(self, approvals):
        with mock.patch.object(verify_approval, "fetch_approvals", lambda *a: approvals):
            return self.run_script(verify_approval)

    def test_requires_an_approval_for_the_environment(self):
        self.assertEqual(self.run_with([]), 1)
        self.assertEqual(self.run_with([{"state": "approved", "environments": [{"name": "other"}], "user": {"login": "x"}}]), 1)

    def test_approved_run_exposes_the_approver(self):
        rc = self.run_with([{"state": "approved", "comment": "CAB-1234 ok", "environments": [{"name": "cfn-change-approval"}],
                             "user": {"login": "lead-engineer"}}])
        self.assertEqual(rc, 0)
        self.assertEqual(self.outputs()["approver"], "lead-engineer")
        self.assertEqual(self.outputs()["approval_comment"], "CAB-1234 ok")

    def test_self_approval_can_be_blocked(self):
        approvals = [{"state": "approved", "environments": [{"name": "cfn-change-approval"}], "user": {"login": "kunal"}}]
        self.assertEqual(self.run_with(approvals), 0)            # warning only by default
        os.environ["BLOCK_SELF_APPROVAL"] = "true"
        self.assertEqual(self.run_with(approvals), 1)
