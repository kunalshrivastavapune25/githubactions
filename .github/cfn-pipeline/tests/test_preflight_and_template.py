import json

from support import SAMPLE_PARAMS, SAMPLE_TEMPLATE, PipelineTestCase

import cfn_template as t
import preflight


class TemplateHelpers(PipelineTestCase):
    def test_loader_understands_short_form_tags_and_keeps_dates_as_strings(self):
        data = t.load_template_text(SAMPLE_TEMPLATE)
        self.assertEqual(data["AWSTemplateFormatVersion"], "2010-09-09")
        props = data["Resources"]["Queue"]["Properties"]
        self.assertEqual(props["VisibilityTimeout"], {"Ref": "Timeout"})
        self.assertEqual(props["QueueName"], {"Fn::Sub": "app-${Env}"})
        self.assertEqual(t.load_template_text("A: !GetAtt Queue.Arn")["A"], {"Fn::GetAtt": ["Queue", "Arn"]})

    def test_resource_snippet_is_the_raw_code_with_comments(self):
        snippet = t.resource_snippet(SAMPLE_TEMPLATE, "Queue")
        self.assertTrue(snippet.startswith("Queue:\n  Type: AWS::SQS::Queue"))
        self.assertIn("!Ref Timeout   # tuned per environment", snippet)
        self.assertNotIn("AppSecurityGroup", snippet)
        sg = t.resource_snippet(SAMPLE_TEMPLATE, "AppSecurityGroup")
        self.assertTrue(sg.rstrip().endswith("CidrIp: 10.0.0.0/8"))   # trailing blank + comment trimmed
        self.assertNotIn("retained on delete", sg)
        self.assertIn("DeletionPolicy: Retain", t.resource_snippet(SAMPLE_TEMPLATE, "Logs"))
        self.assertIsNone(t.resource_snippet(SAMPLE_TEMPLATE, "Missing"))
        self.assertIn("NoEcho: true", t.section_snippet(SAMPLE_TEMPLATE, "Parameters"))

    def test_params_are_checked_against_the_template(self):
        info = t.describe_template(t.load_template_text(SAMPLE_TEMPLATE))
        errors, _ = t.check_params(SAMPLE_PARAMS, info)
        self.assertEqual(errors, [])
        bad = [{"ParameterKey": "Env", "ParameterValue": "qa"}, {"ParameterKey": "Timeout", "ParameterValue": "99999"},
               {"ParameterKey": "Unknown", "ParameterValue": "x"}]
        errors, _ = t.check_params(bad, info)
        joined = " ".join(errors)
        self.assertIn("'Env' value ['qa'] is not in AllowedValues", joined)
        self.assertIn("'Timeout' is above MaxValue", joined)
        self.assertIn("'Unknown', but the template has no such parameter", joined)
        self.assertIn("'DbPassword' has no Default and is missing", joined)

    def test_param_values_keep_spaces_and_commas(self):
        path = self.write("p.json", [{"ParameterKey": "A", "ParameterValue": "a b, c"},
                                     {"ParameterKey": "B", "ParameterValue": 5},
                                     {"ParameterKey": "C", "ParameterValue": True},
                                     {"ParameterKey": "D", "ParameterValue": ["x", "y"]}])
        values = {p["ParameterKey"]: p["ParameterValue"] for p in t.load_params(path)}
        self.assertEqual(values, {"A": "a b, c", "B": "5", "C": "true", "D": "x,y"})

    def test_noecho_values_are_masked_for_display(self):
        info = t.describe_template(t.load_template_text(SAMPLE_TEMPLATE))
        shown = {p["ParameterKey"]: p["ParameterValue"] for p in t.display_params(SAMPLE_PARAMS, info)}
        self.assertEqual(shown["DbPassword"], "****")
        self.assertEqual(shown["Env"], "prd")


class Preflight(PipelineTestCase):
    env = {"FOLDER_PATH": "app", "ROLE_ALIAS": "SANDBOX", "DRIFT_POLICY": "block", "EXECUTION_MODE": "manual-console"}

    def test_happy_path_sets_outputs_and_masks_noecho(self):
        self.sample_stack_files()
        self.assertEqual(self.run_script(preflight), 0)
        out = self.outputs()
        self.assertEqual(out["role_secret"], "SANDBOX_ROLE")
        self.assertEqual(out["tier"], "sandbox")
        self.assertEqual(out["template_file"], "cloudformation/app/app-template.yaml")
        self.assertEqual(out["large_template"], "false")
        self.assertIn("🧭 Preflight", self.summary())
        self.assertNotIn("s3cr3t-value", self.summary())

    def test_full_path_and_path_traversal(self):
        self.sample_stack_files()
        with self.subTest("full path accepted"):
            import os
            os.environ["FOLDER_PATH"] = "cloudformation/app/"
            self.assertEqual(self.run_script(preflight), 0)
        with self.subTest("traversal rejected"):
            os.environ["FOLDER_PATH"] = "../etc"
            self.assertEqual(self.run_script(preflight), 1)

    def test_problems_fail_the_job(self):
        self.write("cloudformation/app/app-template.yaml", SAMPLE_TEMPLATE)
        self.write("cloudformation/app/params.json", [{"ParameterKey": "Env", "ParameterValue": "prd"}])
        import os
        os.environ["STACK_NAME"] = "1-bad_name"
        self.assertEqual(self.run_script(preflight), 1)
        summary = self.summary()
        self.assertIn("stackName '1-bad_name' is invalid", summary)
        self.assertIn("DbPassword", summary)
        self.assertNotIn("role_secret", self.outputs())

    def test_two_templates_in_one_folder_is_ambiguous(self):
        self.sample_stack_files()
        self.write("cloudformation/app/other-template.yml", SAMPLE_TEMPLATE)
        self.assertEqual(self.run_script(preflight), 1)
        self.assertIn("More than one *-template.yaml", self.summary())

    def test_large_template_goes_through_s3(self):
        template, _ = self.sample_stack_files()
        template.write_text(SAMPLE_TEMPLATE + "\n# " + "x" * 52_000 + "\n")
        self.assertEqual(self.run_script(preflight), 0)
        self.assertEqual(self.outputs()["large_template"], "true")

    def test_secret_check(self):
        import os
        os.environ.update({"ROLE_SECRET_NAME": "SANDBOX_ROLE", "ROLE_ARN": "", "NOTIFY_CHANNEL": "ses"})
        self.assertEqual(self.run_script(preflight, "--check-secrets"), 1)
        os.environ.update({"ROLE_ARN": "arn:aws:iam::123456789012:role/deploy", "SERVICES_ROLE_ARN": "arn:aws:iam::1:role/x",
                           "HAS_KIRO_KEY": "false", "AI_PROVIDER": "auto"})
        self.assertEqual(self.run_script(preflight, "--check-secrets"), 0)
        self.assertEqual(self.outputs()["ai_ready"], "bedrock")
        os.environ.update({"HAS_KIRO_KEY": "true"})
        self.run_script(preflight, "--check-secrets")
        self.assertEqual(self.outputs()["ai_ready"], "kiro")

    def test_accounts_config_matches_workflow_dropdown(self):
        import yaml
        from support import ROOT
        workflow = yaml.safe_load((ROOT / ".github/workflows/cfn-deploy-pipeline.yml").read_text())
        options = workflow[True]["workflow_dispatch"]["inputs"]["roleAlias"]["options"]
        aliases = json.loads((ROOT / ".github/cfn-pipeline/config/accounts.json").read_text())["aliases"]
        self.assertEqual(sorted(options), sorted(aliases))
