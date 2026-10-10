"""Runs the stage scripts in pipeline order against fake AWS, and checks the emails people receive.

Set SAVE_SAMPLES=<dir> to keep the rendered emails (used for the README screenshots).
"""
import email
import json
import os
import shutil
from email import policy
from pathlib import Path

from support import FakeClient, PipelineTestCase

import changeset
import common as c
import drift_ai
import drift_detect
import execute
import notify
import report_lint
import report_security
import test_changeset
import test_drift
import test_execute_and_approval

SECRETS = ("s3cr3t-value", "should-not-leak")

FIXES = {
    "Queue": {
        "summary": "The queue's VisibilityTimeout was raised from 30 to 120 seconds outside CloudFormation.",
        "impact": "Failed messages are retried 90 seconds later than the template intends.",
        "recommendation": "ADOPT_LIVE", "risk": "LOW",
        "rationale": "A longer timeout matches the consumer's processing time and has no security impact. The value "
                     "comes from the Timeout parameter, so change params.json rather than hard-coding it.",
        "updated_resource_yaml": "Queue:\n  Type: AWS::SQS::Queue\n  Properties:\n    QueueName: !Sub \"app-${Env}\"\n"
                                 "    VisibilityTimeout: !Ref Timeout   # tuned per environment\n    Tags:\n"
                                 "      - Key: Owner\n        Value: platform",
        "parameter_changes": [{"ParameterKey": "Timeout", "NewValue": "120", "Reason": "Matches the live queue"}],
        "review_notes": ["Apply the same params.json change to every environment that should keep 120 s."],
    },
    "AppSecurityGroup": {
        "summary": "An inbound rule allowing SSH (tcp/22) from 0.0.0.0/0 was added to the security group by hand.",
        "impact": "Port 22 is exposed to the internet on every instance using this group.",
        "recommendation": "REVERT_TO_TEMPLATE", "risk": "HIGH",
        "rationale": "World-open SSH breaks the landing-zone network baseline. Remove the live rule instead of adopting it.",
        "updated_resource_yaml": "AppSecurityGroup:\n  Type: AWS::EC2::SecurityGroup\n  Properties:\n"
                                 "    GroupDescription: App tier\n    VpcId: vpc-0abc\n    SecurityGroupIngress:\n"
                                 "      - IpProtocol: tcp\n        FromPort: 443\n        ToPort: 443\n        CidrIp: 10.0.0.0/8",
        "parameter_changes": [],
        "review_notes": ["If SSH is genuinely needed, use Session Manager or a /32 admin CIDR instead."],
    },
}


def fake_converse(**kwargs):
    prompt = kwargs["messages"][0]["content"][0]["text"]
    for logical_id, fix in FIXES.items():
        if f"Resource: `{logical_id}`" in prompt:
            text = f"{drift_ai.MARK_BEGIN}\n{json.dumps(fix)}\n{drift_ai.MARK_END}"
            break
    else:
        text = "I could not decide."
    return {"output": {"message": {"content": [{"text": text}]}}, "usage": {"inputTokens": 1800, "outputTokens": 350}}


class EndToEnd(PipelineTestCase):
    env = {"TEMPLATE_FILE": "cloudformation/app/app-template.yaml", "PARAMS_FILE": "cloudformation/app/params.json",
           "CHANGE_SET_NAME": "gha-123-1", "MAIL_FROM": "CFN Pipeline <cfn-pipeline@platform.example.com>",
           "MAIL_TO": "platform-team@example.com; cab@example.com", "MAIL_CC": "kunal@example.com",
           "SERVICES_REGION": "eu-west-1", "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-5-5",
           "ROLE_ALIAS": "SHAREDSERVICES-PPE", "TIER": "ppe", "EXECUTION_MODE": "manual-console"}

    def setUp(self):
        super().setUp()
        self.sample_stack_files()
        self.sent = []
        self.clients["sesv2"] = FakeClient(send_email=lambda **kw: self.sent.append(kw) or {"MessageId": "0102-abc"})
        self.clients["sns"] = FakeClient(publish=lambda **kw: self.sent.append(kw) or {"MessageId": "sns-1"})
        self.clients["bedrock-runtime"] = FakeClient(converse=fake_converse)

    def notify(self, kind, channel="ses"):
        os.environ.update({"NOTIFY_CHANNEL": channel, "AWS_ACCESS_KEY_ID": "AKIATEST"})
        self.assertEqual(self.run_script(notify, kind), 0)
        html = (self.dir / f"reports/email/{kind}.html").read_text()
        text = (self.dir / f"reports/email/{kind}.txt").read_text()
        for secret in SECRETS:
            self.assertNotIn(secret, html)
            self.assertNotIn(secret, text)
        target = os.environ.get("SAVE_SAMPLES")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            name = f"{kind}-{os.environ.get('SAMPLE_SUFFIX', '')}".rstrip("-")
            shutil.copy(self.dir / f"reports/email/{kind}.html", Path(target) / f"{name}.html")
        return html, text

    def parsed_mail(self):
        raw = self.sent[-1]["Content"]["Raw"]["Data"]
        return email.message_from_bytes(raw, policy=policy.default)

    # ------------------------------------------------------------------ drift path
    def test_drift_path(self):
        os.environ["DRIFT_POLICY"] = "block"
        test_drift.DriftDetection.make_clients(self)
        self.assertEqual(self.run_script(drift_detect), 0)
        self.assertEqual(self.outputs()["block_reason"], "DRIFT")

        os.environ.update({"AI_PROVIDER": "auto", "AWS_ACCESS_KEY_ID": "AKIATEST"})
        self.run_script(drift_ai)
        suggestions = c.read_json("reports/ai-suggestions.json")["suggestions"]
        self.assertEqual(sum(1 for s in suggestions if s["ok"]), 2)

        os.environ["SAMPLE_SUFFIX"] = ""
        html, text = self.notify("drift")
        mail = self.parsed_mail()
        self.assertEqual(mail["Subject"], "[DRIFT][PPE] demo-stack — deployment halted, 3 drifted resources")
        self.assertEqual(self.sent[-1]["FromEmailAddress"], "cfn-pipeline@platform.example.com")
        self.assertEqual(self.sent[-1]["Destination"], {"ToAddresses": ["platform-team@example.com", "cab@example.com"],
                                                        "CcAddresses": ["kunal@example.com"]})
        names = sorted(part.get_filename() for part in mail.iter_attachments())
        self.assertEqual(names, ["drift-report.json", "drifted-resources-live.yaml", "suggested-code.yaml",
                                 "suggested-fixes.md"])
        for needle in ("Drift detected — deployment halted", "VisibilityTimeout: 120", "Suggested code for "
                       "cloudformation/app/app-template.yaml", "REVERT TO TEMPLATE", "aws cloudtrail lookup-events",
                       "revert-drift", "#/stacks/drifts?stackId=", "Suggested fix · Amazon Bedrock · eu.anthropic.claude-sonnet-5-5"):
            self.assertIn(needle, html)
        self.assertIn("VisibilityTimeout: !Ref Timeout", text)

    # ------------------------------------------------------------------ approval path
    def stage_quality_reports(self):
        lint_raw = self.write("cfn-lint.json", [{
            "Rule": {"Id": "W3005", "Source": "https://github.com/aws-cloudformation/cfn-lint"}, "Level": "Warning",
            "Location": {"Start": {"LineNumber": 22, "ColumnNumber": 5}, "Path": ["Resources", "AppSecurityGroup"]},
            "Message": "Obsolete DependsOn on resource (Queue)", "Filename": "app-template.yaml"}])
        self.assertEqual(self.run_script(report_lint, "--input", str(lint_raw), "--template", "t.yaml",
                                         "--out", "reports/lint-report.json"), 0)
        checkov_raw = self.write("checkov.json", {"check_type": "cloudformation", "results": {"failed_checks": [
            {"check_id": "CKV_AWS_158", "check_name": "Ensure that CloudWatch Log Group is encrypted by KMS",
             "resource": "AWS::Logs::LogGroup.Logs", "file_line_range": [38, 42]}]},
            "summary": {"passed": 6, "failed": 1, "skipped": 0, "parsing_errors": 0}})
        self.assertEqual(self.run_script(report_security, "--input", str(checkov_raw), "--template", "t.yaml",
                                         "--out", "reports/security-report.json"), 0)

    def test_approval_path_manual_console(self):
        test_drift.DriftDetection.make_clients(self, drifts=lambda s: {"StackResourceDrifts": []},
                                    drift_status="IN_SYNC")
        self.run_script(drift_detect)
        self.stage_quality_reports()
        test_changeset.ChangeSetStage.make_cfn(self)
        self.assertEqual(self.run_script(changeset), 0)

        os.environ["SAMPLE_SUFFIX"] = "manual-console"
        html, text = self.notify("approval")
        mail = self.parsed_mail()
        self.assertEqual(mail["Subject"], "[APPROVAL NEEDED][PPE] demo-stack — 7 changes, risk HIGH")
        names = sorted(part.get_filename() for part in mail.iter_attachments())
        self.assertEqual(names, ["changes.csv", "changeset.json"])
        for needle in ("Observation", "Review change set in AWS console", "Why the risk is HIGH", "db.r6g.large",
                       "execute-change-set", "delete-change-set", "In sync", "1 finding · report-only", "0 errors · 1 warning",
                       "Execute change set"):
            self.assertIn(needle, html)
        csv_part = next(p for p in mail.iter_attachments() if p.get_filename() == "changes.csv").get_content()
        self.assertIn("Network/PrivateRoute", csv_part)

    def test_approval_path_with_github_gate_and_result(self):
        os.environ["EXECUTION_MODE"] = "pipeline-after-approval"
        test_drift.DriftDetection.make_clients(self, drifts=lambda s: {"StackResourceDrifts": []}, drift_status="IN_SYNC")
        self.run_script(drift_detect)
        test_changeset.ChangeSetStage.make_cfn(self)
        self.run_script(changeset)
        os.environ["SAMPLE_SUFFIX"] = "github-gate"
        html, _ = self.notify("approval")
        self.assertIn("Review deployments", html)
        self.assertIn("cfn-change-approval", html)

        test_execute_and_approval.Execute.make_cfn(self)
        os.environ.update({"CHANGE_SET_ARN": c.read_json("reports/changeset.json")["change_set_id"], "APPROVER": "lead-engineer"})
        self.assertEqual(self.run_script(execute), 0)
        os.environ["SAMPLE_SUFFIX"] = "success"
        html, _ = self.notify("result")
        self.assertIn("Deployed: demo-stack is UPDATE_COMPLETE", html)
        self.assertIn("lead-engineer", html)
        self.assertEqual(self.parsed_mail()["Subject"], "[DEPLOYED][PPE] demo-stack — UPDATE_COMPLETE")

    def test_failed_deployment_email(self):
        event = test_execute_and_approval.event
        failing = [event(1, "demo-stack", "UPDATE_IN_PROGRESS", "AWS::CloudFormation::Stack"),
                   event(2, "Queue", "UPDATE_FAILED", reason="Resource handler returned message: \"Value 99999 for "
                         "parameter VisibilityTimeout is invalid. Reason: Must be between 0 and 43200.\""),
                   event(3, "AppSecurityGroup", "UPDATE_FAILED", "AWS::EC2::SecurityGroup", reason="Resource update cancelled"),
                   event(4, "demo-stack", "UPDATE_ROLLBACK_COMPLETE", "AWS::CloudFormation::Stack")]
        test_execute_and_approval.Execute.make_cfn(self, final="UPDATE_ROLLBACK_COMPLETE", events=failing)
        os.environ.update({"CHANGE_SET_ARN": "arn:aws:cloudformation:eu-west-1:123456789012:changeSet/gha-123-1/x"})
        self.assertEqual(self.run_script(execute), 1)
        os.environ["SAMPLE_SUFFIX"] = "failed"
        html, _ = self.notify("result")
        self.assertIn("Deployment failed: UPDATE_ROLLBACK_COMPLETE", html)
        self.assertIn("Root cause: Queue", html)

    def test_no_change_email_and_sns_channel(self):
        test_drift.DriftDetection.make_clients(self, drifts=lambda s: {"StackResourceDrifts": []}, drift_status="IN_SYNC")
        self.run_script(drift_detect)
        test_changeset.ChangeSetStage.make_cfn(self, final_status="FAILED", reason="No updates are to be performed.")
        self.run_script(changeset)
        os.environ["SNS_TOPIC_ARN"] = "arn:aws:sns:eu-west-1:123456789012:cfn-pipeline"
        os.environ["SAMPLE_SUFFIX"] = "no-changes"
        self.notify("approval", channel="sns")
        published = self.sent[-1]
        self.assertEqual(published["Subject"], "[NO CHANGES][PPE] demo-stack - nothing to deploy")
        self.assertLessEqual(len(published["Subject"]), 99)
        self.assertIn("nothing to approve", published["Message"])

    def test_sending_problems_do_not_fail_the_job(self):
        test_drift.DriftDetection.make_clients(self, drifts=lambda s: {"StackResourceDrifts": []}, drift_status="IN_SYNC")
        self.run_script(drift_detect)
        test_changeset.ChangeSetStage.make_cfn(self)
        self.run_script(changeset)
        from support import FakeClientError
        self.clients["sesv2"] = FakeClient(send_email=FakeClientError("MessageRejected", "Email address is not verified."))
        os.environ.update({"NOTIFY_CHANNEL": "ses", "AWS_ACCESS_KEY_ID": "AKIATEST"})
        self.assertEqual(self.run_script(notify, "approval"), 0)
        self.assertEqual(self.outputs()["email_sent"], "false")
        self.assertIn("not verified", self.summary())
        os.environ["NOTIFY_STRICT"] = "true"
        self.assertEqual(self.run_script(notify, "approval"), 1)

    def test_drift_stage_crash_still_emails_something_useful(self):
        os.environ.update({"NOTIFY_CHANNEL": "ses", "AWS_ACCESS_KEY_ID": "AKIATEST"})
        self.assertEqual(self.run_script(notify, "drift"), 0)      # no drift report at all
        mail = self.parsed_mail()
        self.assertEqual(mail["Subject"], "[PIPELINE ERROR][PPE] demo-stack — drift check failed")
        self.assertIn("OIDC subject", mail.get_body(("html",)).get_content())

    def test_tool_output_with_log_noise_is_parsed(self):
        noisy = self.write("noisy.json", "[ WARNING ] update available\n" + json.dumps([{
            "Rule": {"Id": "E3012"}, "Level": "Error", "Location": {"Start": {"LineNumber": 7}},
            "Message": "Property VisibilityTimeout should be of type Integer"}]))
        self.assertEqual(self.run_script(report_lint, "--input", str(noisy), "--template", "t.yaml",
                                         "--out", "reports/lint.json"), 1)
        self.assertEqual(c.read_json("reports/lint.json")["errors"], 1)
