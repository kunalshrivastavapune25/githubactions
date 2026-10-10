import json
import os

from support import STACK_ID, T0, FakeClient, FakeClientError, PipelineTestCase, not_found, stack

import common as c
import drift_detect

NESTED_ID = STACK_ID.replace("demo-stack", "demo-stack-Network-ABC")


def drifts_for(stack_id_or_name, include_nested_delete=True):
    if stack_id_or_name == NESTED_ID:
        return {"StackResourceDrifts": [{
            "StackId": NESTED_ID, "LogicalResourceId": "PrivateRoute", "PhysicalResourceId": "rtb-1|0.0.0.0/0",
            "ResourceType": "AWS::EC2::Route", "StackResourceDriftStatus": "DELETED", "Timestamp": T0,
            "ExpectedProperties": json.dumps({"RouteTableId": "rtb-1", "DestinationCidrBlock": "0.0.0.0/0"}),
            "PropertyDifferences": []}] if include_nested_delete else []}
    return {"StackResourceDrifts": [
        {"StackId": STACK_ID, "LogicalResourceId": "Queue", "ResourceType": "AWS::SQS::Queue",
         "PhysicalResourceId": "https://sqs.eu-west-1.amazonaws.com/123456789012/app-prd",
         "StackResourceDriftStatus": "MODIFIED", "Timestamp": T0,
         "ExpectedProperties": json.dumps({"QueueName": "app-prd", "VisibilityTimeout": 30}),
         "ActualProperties": json.dumps({"QueueName": "app-prd", "VisibilityTimeout": 120,
                                         "RedrivePolicy": {"maxReceiveCount": 5}}),
         "PropertyDifferences": [{"PropertyPath": "/VisibilityTimeout", "ExpectedValue": "30", "ActualValue": "120",
                                  "DifferenceType": "NOT_EQUAL"}]},
        {"StackId": STACK_ID, "LogicalResourceId": "AppSecurityGroup", "ResourceType": "AWS::EC2::SecurityGroup",
         "PhysicalResourceId": "sg-0123", "StackResourceDriftStatus": "MODIFIED", "Timestamp": T0,
         "ExpectedProperties": json.dumps({"SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                                                                      "CidrIp": "10.0.0.0/8"}]}),
         "ActualProperties": json.dumps({"SecurityGroupIngress": [
             {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "10.0.0.0/8"},
             {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "CidrIp": "0.0.0.0/0"}],
             "Tags": [{"Key": "api_key", "Value": "should-not-leak"}]}),
         "PropertyDifferences": [{"PropertyPath": "/SecurityGroupIngress/1", "ExpectedValue": "null",
                                  "ActualValue": json.dumps({"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
                                                             "CidrIp": "0.0.0.0/0"}), "DifferenceType": "ADD"}]},
        {"StackId": STACK_ID, "LogicalResourceId": "Logs", "ResourceType": "AWS::Logs::LogGroup",
         "PhysicalResourceId": "app-logs", "StackResourceDriftStatus": "IN_SYNC", "Timestamp": T0},
    ]}


class DriftDetection(PipelineTestCase):
    env = {"DRIFT_POLICY": "block", "TEMPLATE_FILE": "cloudformation/app/app-template.yaml"}

    def make_clients(self, status="UPDATE_COMPLETE", drifts=drifts_for, detection="DETECTION_COMPLETE",
                     drift_status="DRIFTED", nested=True):
        progress = {"det-root": ["DETECTION_IN_PROGRESS", detection], "det-nested": [detection]}

        def detection_status(StackDriftDetectionId):
            states = progress[StackDriftDetectionId]
            state = states.pop(0) if len(states) > 1 else states[0]
            return {"StackDriftDetectionId": StackDriftDetectionId, "DetectionStatus": state,
                    "StackDriftStatus": drift_status if state != "DETECTION_IN_PROGRESS" else None,
                    "DetectionStatusReason": "Failed to detect drift on resource [X]" if state == "DETECTION_FAILED" else None,
                    "Timestamp": T0}

        def resources(StackName, **_):
            if StackName == STACK_ID and nested:
                return {"StackResourceSummaries": [
                    {"LogicalResourceId": "Network", "ResourceType": "AWS::CloudFormation::Stack",
                     "PhysicalResourceId": NESTED_ID, "ResourceStatus": "UPDATE_COMPLETE"},
                    {"LogicalResourceId": "Queue", "ResourceType": "AWS::SQS::Queue", "PhysicalResourceId": "q",
                     "ResourceStatus": "UPDATE_COMPLETE"}]}
            return {"StackResourceSummaries": []}

        self.clients["cloudformation"] = FakeClient(
            describe_stacks={"Stacks": [stack(status)]} if status else not_found,
            list_stack_resources=resources,
            detect_stack_drift=lambda StackName: {"StackDriftDetectionId": "det-root" if StackName == STACK_ID else "det-nested"},
            describe_stack_drift_detection_status=detection_status,
            describe_stack_resource_drifts=lambda StackName, **_: drifts(StackName),
            describe_type={"Schema": json.dumps({"readOnlyProperties": ["/properties/Arn", "/properties/QueueUrl"]})},
        )
        self.clients["cloudcontrol"] = FakeClient(get_resource={"ResourceDescription": {"Properties": json.dumps(
            {"QueueName": "app-prd", "VisibilityTimeout": 120, "Arn": "arn:aws:sqs:eu-west-1:1:app-prd",
             "QueueUrl": "https://sqs...", "SqsManagedSseEnabled": True})}})

    def setUp(self):
        super().setUp()
        self.sample_stack_files()

    def test_drift_blocks_and_captures_everything_amazon_q_needs(self):
        self.make_clients()
        self.assertEqual(self.run_script(drift_detect, "--out", "reports/drift.json"), 0)
        out = self.outputs()
        self.assertEqual((out["gate"], out["block_reason"], out["drift_status"]), ("block", "DRIFT", "DRIFTED"))
        self.assertEqual(out["drifted_count"], "3")
        report = c.read_json("reports/drift.json")
        by_id = {d["logical_id"]: d for d in report["drifted_resources"]}
        queue = by_id["Queue"]
        self.assertIn("VisibilityTimeout: 120", queue["live_yaml"])
        self.assertTrue(queue["live_yaml"].startswith("Queue:\n  Type: AWS::SQS::Queue\n  Properties:"))
        self.assertIn("!Ref Timeout", queue["template_snippet"])
        self.assertEqual(queue["differences"][0], {"path": "/VisibilityTimeout", "expected": 30, "actual": 120,
                                                   "type": "NOT_EQUAL"})
        self.assertTrue(queue["live_state"]["available"])
        self.assertNotIn("Arn:", queue["live_state"]["yaml"])            # read-only attribute stripped
        self.assertIn("SqsManagedSseEnabled: true", queue["live_state"]["yaml"])
        sg = by_id["AppSecurityGroup"]
        self.assertNotIn("should-not-leak", json.dumps(sg))              # secret-looking tag value redacted
        self.assertEqual(sg["differences"][0]["actual"]["CidrIp"], "0.0.0.0/0")
        route = by_id["PrivateRoute"]
        self.assertEqual((route["status"], route["is_root_stack"], route["template_snippet"]), ("DELETED", False, None))
        self.assertEqual(len(self.clients["cloudformation"].called("detect_stack_drift")), 2)  # root + nested
        self.assertIn("Drift detection — ❌ DRIFTED", self.summary())

    def test_in_sync_passes(self):
        self.make_clients(drifts=lambda s: drifts_for(s, False) if s == NESTED_ID else {"StackResourceDrifts": []},
                          drift_status="IN_SYNC")
        self.run_script(drift_detect)
        out = self.outputs()
        self.assertEqual((out["gate"], out["drift_status"], out["change_set_type"]), ("pass", "IN_SYNC", "UPDATE"))

    def test_new_stack_skips_drift(self):
        self.make_clients(status=None)
        self.run_script(drift_detect)
        out = self.outputs()
        self.assertEqual((out["gate"], out["drift_status"], out["change_set_type"]), ("pass", "NEW_STACK", "CREATE"))
        self.assertEqual(self.clients["cloudformation"].called("detect_stack_drift"), [])

    def test_review_in_progress_is_treated_as_new(self):
        self.make_clients(status="REVIEW_IN_PROGRESS")
        self.run_script(drift_detect)
        self.assertEqual(self.outputs()["change_set_type"], "CREATE")

    def test_unrecoverable_stack_state_blocks_with_guidance(self):
        self.make_clients(status="UPDATE_ROLLBACK_FAILED")
        self.run_script(drift_detect)
        out = self.outputs()
        self.assertEqual((out["gate"], out["block_reason"]), ("block", "STACK_STATE"))
        self.assertIn("continue-update-rollback", c.read_json("reports/drift-report.json")["message"])

    def test_revert_policy_lets_the_run_continue(self):
        os.environ["DRIFT_POLICY"] = "revert-drift"
        self.make_clients()
        self.run_script(drift_detect)
        out = self.outputs()
        self.assertEqual((out["gate"], out["revert_drift"], out["drift_status"]), ("pass", "true", "DRIFTED"))

    def test_failed_detection_without_drift_is_inconclusive(self):
        self.make_clients(drifts=lambda s: {"StackResourceDrifts": []}, detection="DETECTION_FAILED",
                          drift_status="IN_SYNC", nested=False)
        self.run_script(drift_detect)
        out = self.outputs()
        self.assertEqual((out["gate"], out["block_reason"], out["drift_status"]), ("block", "INCONCLUSIVE", "UNKNOWN"))

    def test_api_error_writes_report_and_fails(self):
        self.make_clients()
        self.clients["cloudformation"] = FakeClient(
            describe_stacks=FakeClientError("AccessDenied", "not authorized to perform cloudformation:DescribeStacks"))
        self.assertEqual(self.run_script(drift_detect), 1)
        report = c.read_json("reports/drift-report.json")
        self.assertEqual((report["gate"], report["block_reason"]), ("block", "ERROR"))
        self.assertIn("not authorized", report["error"])
