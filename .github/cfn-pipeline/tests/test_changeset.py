import os

from support import CS_ID, STACK_ID, T0, FakeClient, FakeClientError, PipelineTestCase, not_found, stack

import changeset
import common as c

NESTED_CS = CS_ID.replace("gha-123-1", "gha-123-1-Network-XYZ")


def change(action, logical, rtype, replacement=None, details=(), **extra):
    rc = {"Action": action, "LogicalResourceId": logical, "ResourceType": rtype, "Details": list(details)}
    if replacement:
        rc["Replacement"] = replacement
    if action != "Add":
        rc["PhysicalResourceId"] = f"{logical.lower()}-id"
    rc.update(extra)
    return {"Type": "Resource", "ResourceChange": rc}


def detail(name, before, after, recreation="Never", source="DirectModification"):
    return {"Target": {"Attribute": "Properties", "Name": name, "Path": f"/Properties/{name}", "RequiresRecreation": recreation,
                       "BeforeValue": before, "AfterValue": after, "AttributeChangeType": "Modify"},
            "Evaluation": "Static", "ChangeSource": source}


PAGE_1 = [
    change("Modify", "Database", "AWS::RDS::DBInstance", "Conditional",
           [detail("DBInstanceClass", "db.t3.medium", "db.r6g.large", "Conditionally")]),
    change("Modify", "AppSecurityGroup", "AWS::EC2::SecurityGroup", "False",
           [detail("SecurityGroupIngress", '[{"CidrIp":"10.0.0.0/8","FromPort":443}]', '[{"CidrIp":"10.0.0.0/8","FromPort":8443}]')]),
    change("Add", "AppRole", "AWS::IAM::Role", AfterContext='{"Properties":{"RoleName":"app"}}'),
]
PAGE_2 = [
    change("Remove", "OldLogs", "AWS::Logs::LogGroup", PolicyAction="Retain"),
    change("Modify", "Queue", "AWS::SQS::Queue", "False", [detail("VisibilityTimeout", "30", "60", source="ParameterReference")]),
    change("Modify", "Network", "AWS::CloudFormation::Stack", "False", ChangeSetId=NESTED_CS),
]
NESTED = [change("Modify", "PrivateRoute", "AWS::EC2::Route", "True", [detail("NatGatewayId", "nat-1", "nat-2", "Always")])]


class ChangeSetStage(PipelineTestCase):
    env = {"TEMPLATE_FILE": "cloudformation/app/app-template.yaml", "PARAMS_FILE": "cloudformation/app/params.json",
           "CHANGE_SET_NAME": "gha-123-1", "REVERT_DRIFT": "false"}

    def setUp(self):
        super().setUp()
        self.sample_stack_files()

    def make_cfn(self, existing=True, final_status="CREATE_COMPLETE", reason="", events=None):
        status_seq = ["CREATE_IN_PROGRESS", final_status]

        def describe_change_set(ChangeSetName, IncludePropertyValues=False, NextToken=None):
            if ChangeSetName == NESTED_CS:
                return {"ChangeSetId": NESTED_CS, "Status": "CREATE_COMPLETE", "Changes": NESTED}
            if not IncludePropertyValues:
                state = status_seq.pop(0) if len(status_seq) > 1 else status_seq[0]
                return {"ChangeSetId": CS_ID, "StackId": STACK_ID, "Status": state, "StatusReason": reason,
                        "ExecutionStatus": "AVAILABLE" if state == "CREATE_COMPLETE" else "UNAVAILABLE"}
            if NextToken is None:
                return {"ChangeSetId": CS_ID, "StackId": STACK_ID, "Status": "CREATE_COMPLETE", "ExecutionStatus": "AVAILABLE",
                        "Changes": PAGE_1, "NextToken": "page-2"}
            return {"ChangeSetId": CS_ID, "Changes": PAGE_2}

        handlers = dict(
            describe_stacks={"Stacks": [stack("UPDATE_COMPLETE", EnableTerminationProtection=True,
                                              Parameters=[{"ParameterKey": "Timeout", "ParameterValue": "45"}])]}
            if existing else not_found,
            validate_template={"Capabilities": ["CAPABILITY_NAMED_IAM"], "CapabilitiesReason": "The template contains IAM resources"},
            get_stack_policy={"StackPolicyBody": '{"Statement":[]}'},
            list_change_sets={"Summaries": [{"ChangeSetName": "manual-fix", "ExecutionStatus": "AVAILABLE"}]},
            create_change_set={"Id": CS_ID, "StackId": STACK_ID},
            describe_change_set=describe_change_set,
            delete_change_set={},
            describe_events={"OperationEvents": events or []},
        )
        self.clients["cloudformation"] = FakeClient(**handlers)
        return self.clients["cloudformation"]

    def test_update_change_set_with_risk_assessment(self):
        warn = {"EventType": "VALIDATION_ERROR", "ValidationName": "BUCKET_EMPTINESS_VALIDATION", "ValidationStatus": "FAILED",
                "ValidationFailureMode": "WARN", "LogicalResourceId": "OldBucket", "ValidationStatusReason": "Bucket is not empty"}
        cfn = self.make_cfn(events=[warn])
        self.assertEqual(self.run_script(changeset), 0)
        created = cfn.called("create_change_set")[0]
        self.assertEqual(created["ChangeSetType"], "UPDATE")
        self.assertEqual(created["ChangeSetName"], "gha-123-1")
        self.assertIn("CAPABILITY_NAMED_IAM", created["Capabilities"])
        self.assertIn("TemplateBody", created)
        self.assertNotIn("DeploymentMode", created)
        self.assertNotIn("IncludeNestedStacks", created)   # sample template has no nested stacks
        self.assertEqual({p["ParameterKey"]: p["ParameterValue"] for p in created["Parameters"]}["Timeout"], "30")

        report = c.read_json("reports/changeset.json")
        self.assertEqual(report["status"], "CREATE_COMPLETE")
        rows = {r["logical_id"]: r for r in report["changes"]}
        self.assertEqual(rows["Database"]["risk"], "HIGH")                 # stateful + conditional replacement
        self.assertEqual(rows["AppSecurityGroup"]["risk"], "MEDIUM")       # security sensitive
        self.assertEqual(rows["AppRole"]["risk"], "MEDIUM")
        self.assertEqual(rows["OldLogs"]["risk"], "MEDIUM")                # removed but retained
        self.assertEqual(rows["Queue"]["risk"], "LOW")
        self.assertEqual(rows["Network/PrivateRoute"]["replacement"], "True")  # nested change set flattened
        self.assertEqual(report["risk"], "HIGH")
        self.assertEqual(report["replacements"], 2)
        self.assertEqual(rows["Queue"]["details"][0]["after"], 60)
        self.assertEqual(report["validation_events"][0]["mode"], "WARN")
        notes = " ".join(report["context_notes"])
        self.assertIn("stack policy", notes)
        self.assertIn("manual-fix", notes)

        out = self.outputs()
        self.assertEqual((out["status"], out["risk"], out["change_set_arn"]), ("CREATE_COMPLETE", "HIGH", CS_ID))
        self.assertIn("changesets/changes?stackId=arn%3Aaws%3Acloudformation", out["console_url"])
        summary = self.summary()
        self.assertIn("🔴 risk HIGH", summary)
        self.assertIn("execute-change-set", summary)

    def test_new_stack_gets_create_change_set(self):
        cfn = self.make_cfn(existing=False)
        self.run_script(changeset)
        self.assertEqual(cfn.called("create_change_set")[0]["ChangeSetType"], "CREATE")
        self.assertEqual(cfn.called("get_stack_policy"), [])

    def test_no_changes_is_green_and_cleans_up(self):
        cfn = self.make_cfn(final_status="FAILED",
                            reason="The submitted information didn't contain changes. Submit different information to create a change set.")
        self.assertEqual(self.run_script(changeset), 0)
        self.assertEqual(self.outputs()["status"], "NO_CHANGES")
        self.assertEqual(cfn.called("delete_change_set")[0]["ChangeSetName"], CS_ID)
        self.assertIn("no changes", self.summary())

    def test_validation_failure_shows_the_exact_errors(self):
        failure = {"EventType": "VALIDATION_ERROR", "ValidationName": "PROPERTY_VALIDATION", "ValidationStatus": "FAILED",
                   "ValidationFailureMode": "FAIL", "LogicalResourceId": "Queue",
                   "ValidationPath": "/Resources/Queue/Properties/VisibilityTimeout",
                   "ValidationStatusReason": "#/VisibilityTimeout: expected maximum: 43200, found: 99999"}
        cfn = self.make_cfn(final_status="FAILED", events=[failure],
                            reason="The following hook(s)/validation failed: [AWS::EarlyValidation::PropertyValidation]")
        self.assertEqual(self.run_script(changeset), 1)
        self.assertEqual(cfn.called("describe_events")[0]["Filters"], {"FailedEvents": True})
        report = c.read_json("reports/changeset.json")
        self.assertEqual(report["validation_events"][0]["path"], "/Resources/Queue/Properties/VisibilityTimeout")
        self.assertIn("expected maximum: 43200", self.summary())

    def test_revert_drift_and_large_template(self):
        os.environ["REVERT_DRIFT"] = "true"
        self.write("template_url", "https://ss-cfts3.s3.eu-west-1.amazonaws.com/demo.yaml?X-Amz-Signature=abc")
        cfn = self.make_cfn()
        self.run_script(changeset, "--template-url-file", "template_url")
        created = cfn.called("create_change_set")[0]
        self.assertEqual(created["DeploymentMode"], "REVERT_DRIFT")
        self.assertTrue(created["TemplateURL"].startswith("https://ss-cfts3"))
        self.assertNotIn("TemplateBody", created)
        self.assertEqual(cfn.called("validate_template")[0], {"TemplateURL": created["TemplateURL"]})

    def test_api_rejection_is_reported_cleanly(self):
        cfn = self.make_cfn()
        cfn._handlers["create_change_set"] = FakeClientError("ValidationError", "Parameters: [Env] must have values")
        self.assertEqual(self.run_script(changeset), 1)
        self.assertEqual(self.outputs()["status"], "FAILED")
        self.assertIn("must have values", c.read_json("reports/changeset.json")["status_reason"])

    def test_parameter_missing_from_params_json_keeps_deployed_value(self):
        self.write("cloudformation/app/params.json", [{"ParameterKey": "Env", "ParameterValue": "prd"},
                                                      {"ParameterKey": "DbPassword", "ParameterValue": "x"}])
        cfn = self.make_cfn()
        self.run_script(changeset)
        params = cfn.called("create_change_set")[0]["Parameters"]
        self.assertIn({"ParameterKey": "Timeout", "UsePreviousValue": True}, params)
        self.assertIn("deployed value is kept: Timeout", " ".join(c.read_json("reports/changeset.json")["context_notes"]))

    def test_rerun_attempt_gets_a_new_change_set_name(self):
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        cfn = self.make_cfn()
        self.run_script(changeset)
        self.assertEqual(cfn.called("create_change_set")[0]["ChangeSetName"], "gha-123-2")
