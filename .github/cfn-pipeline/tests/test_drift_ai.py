import json
import os
import subprocess
from unittest import mock

from support import SAMPLE_TEMPLATE, FakeClient, FakeClientError, PipelineTestCase

import cfn_template as t
import common as c
import drift_ai

QUEUE_FIX = {
    "summary": "VisibilityTimeout was raised from 30 to 120 seconds in the console.",
    "impact": "Messages stay invisible longer; slower retries.",
    "recommendation": "ADOPT_LIVE",
    "risk": "LOW",
    "rationale": "Matches the longer processing time of the consumer.",
    "updated_resource_yaml": "Queue:\n  Type: AWS::SQS::Queue\n  Properties:\n    QueueName: !Sub \"app-${Env}\"\n"
                             "    VisibilityTimeout: !Ref Timeout   # tuned per environment\n",
    "parameter_changes": [{"ParameterKey": "Timeout", "NewValue": "120", "Reason": "Value comes from the parameter"}],
    "review_notes": ["Update params.json for every environment"],
}


def drift_report():
    return {
        "stack_name": "demo-stack", "region": "eu-west-1", "template_file": "cloudformation/app/app-template.yaml",
        "drifted_resources": [{
            "stack": "demo-stack", "is_root_stack": True, "logical_id": "Queue", "resource_type": "AWS::SQS::Queue",
            "physical_id": "https://sqs/app-prd", "status": "MODIFIED",
            "differences": [{"path": "/VisibilityTimeout", "expected": 30, "actual": 120, "type": "NOT_EQUAL"}],
            "live_yaml": "Queue:\n  Type: AWS::SQS::Queue\n  Properties:\n    VisibilityTimeout: 120\n",
            "template_snippet": t.resource_snippet(SAMPLE_TEMPLATE, "Queue"),
            "live_state": {"available": True, "yaml": "QueueName: app-prd\nVisibilityTimeout: 120\n"},
        }],
    }


def wrapped(payload: dict) -> str:
    return f"Thinking...\n{drift_ai.MARK_BEGIN}\n{json.dumps(payload)}\n{drift_ai.MARK_END}\n"


class Parsing(PipelineTestCase):
    def test_extracts_marked_json_even_with_ansi_codes(self):
        text = "\x1b[32m> \x1b[0m" + wrapped(QUEUE_FIX) + "\x1b[2m▸ Credits: 0.05 • Time: 6s\x1b[0m"
        self.assertEqual(drift_ai.extract_json(text)["recommendation"], "ADOPT_LIVE")

    def test_falls_back_to_fenced_json(self):
        self.assertEqual(drift_ai.extract_json("Here you go:\n```json\n" + json.dumps(QUEUE_FIX) + "\n```")["risk"], "LOW")

    def test_literal_newlines_inside_strings_are_tolerated(self):
        raw = wrapped(QUEUE_FIX).replace("\\n", "\n")
        self.assertIn("VisibilityTimeout", drift_ai.extract_json(raw)["updated_resource_yaml"])

    def test_echoed_prompt_template_is_not_mistaken_for_an_answer(self):
        prompt = drift_ai.build_prompt(drift_report()["drifted_resources"][0], {
            "stack": "s", "region": "r", "alias": "a", "tier": "t", "template_file": "f"})
        self.assertIsNone(drift_ai.extract_json(prompt))

    def test_normalize_validates_the_code(self):
        good = drift_ai.normalize(dict(QUEUE_FIX, updated_resource_yaml="```yaml\n" + QUEUE_FIX["updated_resource_yaml"] + "```"), "Queue")
        self.assertTrue(good["yaml_valid"])
        self.assertTrue(good["updated_resource_yaml"].startswith("Queue:"))
        bad = drift_ai.normalize(dict(QUEUE_FIX, recommendation="yolo", risk="?", updated_resource_yaml="Queue: ["), "Queue")
        self.assertEqual((bad["recommendation"], bad["risk"], bad["yaml_valid"]), ("NEEDS_REVIEW", "MEDIUM", False))
        self.assertTrue(any("Code check" in n for n in bad["review_notes"]))

    def test_prompt_contains_repo_code_params_and_live_state_but_no_secrets(self):
        ctx = {"stack": "demo-stack", "region": "eu-west-1", "alias": "SANDBOX", "tier": "sandbox",
               "template_file": "app-template.yaml", "parameters_snippet": "Parameters: ...",
               "params": [{"ParameterKey": "DbPassword", "ParameterValue": "****"}]}
        prompt = drift_ai.build_prompt(drift_report()["drifted_resources"][0], ctx)
        for needle in ("!Ref Timeout", "/VisibilityTimeout", "Cloud Control", "\"****\"", "Queue:` at column 0"):
            self.assertIn(needle, prompt)


class Providers(PipelineTestCase):
    env = {"TEMPLATE_FILE": "cloudformation/app/app-template.yaml", "PARAMS_FILE": "cloudformation/app/params.json"}

    def setUp(self):
        super().setUp()
        self.sample_stack_files()
        self.write("reports/drift-report.json", drift_report())

    def test_bedrock_suggestion_end_to_end(self):
        os.environ.update({"AI_PROVIDER": "auto", "AWS_ACCESS_KEY_ID": "AKIATEST", "BEDROCK_MODEL_ID": "eu.anthropic.claude-sonnet-5-5"})
        self.clients["bedrock-runtime"] = FakeClient(converse={
            "output": {"message": {"content": [{"text": wrapped(QUEUE_FIX)}]}},
            "usage": {"inputTokens": 2100, "outputTokens": 400}, "stopReason": "end_turn"})
        self.assertEqual(self.run_script(drift_ai), 0)
        call = self.clients["bedrock-runtime"].called("converse")[0]
        self.assertEqual(call["modelId"], "eu.anthropic.claude-sonnet-5-5")
        prompt = call["messages"][0]["content"][0]["text"]
        self.assertIn("\"****\"", prompt)                      # NoEcho param masked
        self.assertNotIn("s3cr3t-value", prompt)
        report = c.read_json("reports/ai-suggestions.json")
        s = report["suggestions"][0]
        self.assertTrue(s["ok"] and s["yaml_valid"])
        self.assertEqual((s["provider"], s["recommendation"]), ("bedrock", "ADOPT_LIVE"))
        md = (self.dir / "reports/suggested-fixes.md").read_text()
        self.assertIn("VisibilityTimeout: !Ref Timeout", md)
        self.assertEqual(self.outputs()["suggestions"], "1")

    def test_kiro_headless_is_preferred_and_reads_the_prompt_from_stdin(self):
        os.environ.update({"AI_PROVIDER": "auto", "KIRO_API_KEY": "ksk_test", "AWS_ACCESS_KEY_ID": "AKIATEST"})
        seen = {}

        def fake_run(cmd, input, **kwargs):
            seen.update(cmd=cmd, input=input, env=kwargs["env"])
            return subprocess.CompletedProcess(cmd, 0, stdout="\x1b[1m" + wrapped(QUEUE_FIX), stderr="")

        with mock.patch.object(drift_ai.shutil, "which", lambda name: "/home/runner/.local/bin/kiro-cli" if name == "kiro-cli" else None), \
                mock.patch.object(drift_ai.subprocess, "run", fake_run):
            self.run_script(drift_ai)
        self.assertEqual(seen["cmd"][:3], ["/home/runner/.local/bin/kiro-cli", "chat", "--no-interactive"])
        self.assertIn("--trust-tools=read,grep", seen["cmd"])
        self.assertIn("!Ref Timeout", seen["input"])
        self.assertEqual(seen["env"]["NO_COLOR"], "1")
        s = c.read_json("reports/ai-suggestions.json")["suggestions"][0]
        self.assertEqual((s["provider"], s["provider_label"]), ("kiro", "Amazon Q Developer (Kiro CLI)"))

    def test_falls_back_to_bedrock_when_kiro_fails(self):
        os.environ.update({"AI_PROVIDER": "auto", "KIRO_API_KEY": "ksk_test", "AWS_ACCESS_KEY_ID": "AKIATEST"})
        self.clients["bedrock-runtime"] = FakeClient(converse={"output": {"message": {"content": [{"text": wrapped(QUEUE_FIX)}]}}})
        failing = subprocess.CompletedProcess([], 1, stdout="", stderr="error: API key is not valid")
        with mock.patch.object(drift_ai.shutil, "which", lambda name: "/bin/kiro-cli" if name == "kiro-cli" else None), \
                mock.patch.object(drift_ai.subprocess, "run", lambda *a, **k: failing):
            self.run_script(drift_ai)
        s = c.read_json("reports/ai-suggestions.json")["suggestions"][0]
        self.assertEqual(s["provider"], "bedrock")

    def test_provider_errors_are_reported_not_fatal(self):
        os.environ.update({"AI_PROVIDER": "bedrock", "AWS_ACCESS_KEY_ID": "AKIATEST"})
        self.clients["bedrock-runtime"] = FakeClient(converse=FakeClientError(
            "AccessDeniedException", "You don't have access to the model with the specified model ID."))
        self.assertEqual(self.run_script(drift_ai), 0)
        s = c.read_json("reports/ai-suggestions.json")["suggestions"][0]
        self.assertFalse(s["ok"])
        self.assertIn("don't have access", s["error"])

    def test_no_provider_configured(self):
        os.environ["AI_PROVIDER"] = "auto"
        self.assertEqual(self.run_script(drift_ai), 0)
        report = c.read_json("reports/ai-suggestions.json")
        self.assertEqual(report["suggestions"], [])
        self.assertIn("No AI provider configured", report["note"])

    def test_a_failing_provider_is_not_retried_for_every_resource(self):
        report = drift_report()
        report["drifted_resources"].append(dict(report["drifted_resources"][0], logical_id="Queue2"))
        self.write("reports/drift-report.json", report)
        os.environ.update({"AI_PROVIDER": "auto", "KIRO_API_KEY": "ksk_bad", "AWS_ACCESS_KEY_ID": "AKIATEST"})
        self.clients["bedrock-runtime"] = FakeClient(converse={"output": {"message": {"content": [{"text": wrapped(QUEUE_FIX)}]}}})
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            return subprocess.CompletedProcess([], 1, stdout="", stderr="error: invalid API key")

        with mock.patch.object(drift_ai.shutil, "which", lambda name: "/bin/kiro-cli" if name == "kiro-cli" else None), \
                mock.patch.object(drift_ai.subprocess, "run", failing):
            self.run_script(drift_ai)
        self.assertEqual(len(calls), 1)                                   # Kiro tried once only
        providers = [s["provider"] for s in c.read_json("reports/ai-suggestions.json")["suggestions"]]
        self.assertEqual(providers, ["bedrock", "bedrock"])
