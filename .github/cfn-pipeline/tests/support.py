"""Test helpers: fake AWS clients and a TestCase that mimics the GitHub Actions runner."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]          # repository root
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)
ACCOUNT = "123456789012"
STACK_ID = f"arn:aws:cloudformation:eu-west-1:{ACCOUNT}:stack/demo-stack/11111111-2222-3333-4444-555555555555"
CS_ID = f"arn:aws:cloudformation:eu-west-1:{ACCOUNT}:changeSet/gha-123-1/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

SAMPLE_TEMPLATE = """AWSTemplateFormatVersion: "2010-09-09"
Description: Sample stack used by the unit tests
Parameters:
  Env:
    Type: String
    AllowedValues: [dev, prd]
  Timeout:
    Type: Number
    Default: 30
    MinValue: 0
    MaxValue: 43200
  DbPassword:
    Type: String
    NoEcho: true
Resources:
  Queue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub "app-${Env}"
      VisibilityTimeout: !Ref Timeout   # tuned per environment
      Tags:
        - Key: Owner
          Value: platform
  AppSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: App tier
      VpcId: vpc-0abc
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          CidrIp: 10.0.0.0/8

  # retained on delete
  Logs:
    Type: AWS::Logs::LogGroup
    DeletionPolicy: Retain
    Properties:
      RetentionInDays: 14
Outputs:
  QueueUrl:
    Value: !Ref Queue
"""

SAMPLE_PARAMS = [
    {"ParameterKey": "Env", "ParameterValue": "prd"},
    {"ParameterKey": "Timeout", "ParameterValue": "30"},
    {"ParameterKey": "DbPassword", "ParameterValue": "s3cr3t-value"},
]


class FakeClientError(Exception):
    """Same shape as botocore.exceptions.ClientError (only .response is used)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeClient:
    """Records calls; each method returns a value, a callable result, or the next item of a list."""

    def __init__(self, **handlers):
        self.calls: list[tuple[str, dict]] = []
        self._handlers = handlers

    def __getattr__(self, name):
        if name.startswith("_") or name not in self._handlers:
            raise AttributeError(name)

        def method(**kwargs):
            self.calls.append((name, kwargs))
            handler = self._handlers[name]
            if isinstance(handler, list):  # sequence: pop until one item left
                value = handler.pop(0) if len(handler) > 1 else handler[0]
            else:
                value = handler
            if callable(value):
                value = value(**kwargs)
            if isinstance(value, Exception):
                raise value
            return value

        return method

    def called(self, name: str) -> list[dict]:
        return [kw for n, kw in self.calls if n == name]


class PipelineTestCase(unittest.TestCase):
    """Temp working dir + GITHUB_OUTPUT/GITHUB_STEP_SUMMARY files + fake clients."""

    env: dict[str, str] = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.output_file = self.dir / "github_output"
        self.summary_file = self.dir / "github_summary"
        self.output_file.touch()
        self.summary_file.touch()
        base = {
            "GITHUB_OUTPUT": str(self.output_file), "GITHUB_STEP_SUMMARY": str(self.summary_file),
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "plat-eng/AWS-Landing-Zone",
            "GITHUB_SERVER_URL": "https://tescoims.ghe.com", "GITHUB_SHA": "abcdef1234567890",
            "GITHUB_ACTOR": "kunal", "GITHUB_WORKFLOW": "CloudFormation Deploy Pipeline",
            "CFN_POLL_SECONDS": "0", "STACK_NAME": "demo-stack", "TARGET_REGION": "eu-west-1",
            "ROLE_ALIAS": "SANDBOX", "TIER": "sandbox", "NOTIFY_CHANNEL": "none",
        }
        base.update(self.env)
        self._env_patch = mock.patch.dict(os.environ, base)
        self._env_patch.start()
        for key in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "KIRO_API_KEY",
                    "CFN_OIDC_ROLE_ARN", "ACTIONS_ID_TOKEN_REQUEST_URL"):
            os.environ.pop(key, None)
        self.clients: dict[str, FakeClient] = {}
        common.CLIENT_FACTORY = lambda service, region: self.clients[service]
        self._cwd = os.getcwd()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self._cwd)
        common.CLIENT_FACTORY = None
        self._env_patch.stop()
        self._tmp.cleanup()

    # ---- helpers
    def write(self, rel: str, content) -> Path:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return path

    def sample_stack_files(self, folder: str = "cloudformation/app") -> tuple[Path, Path]:
        template = self.write(f"{folder}/app-template.yaml", SAMPLE_TEMPLATE)
        params = self.write(f"{folder}/params.json", SAMPLE_PARAMS)
        return template, params

    def outputs(self) -> dict[str, str]:
        result, lines, i = {}, self.output_file.read_text().splitlines(), 0
        while i < len(lines):
            line = lines[i]
            if "<<" in line and "=" not in line.split("<<")[0]:
                name, delim = line.split("<<", 1)
                body = []
                i += 1
                while lines[i] != delim:
                    body.append(lines[i])
                    i += 1
                result[name] = "\n".join(body)
            elif "=" in line:
                name, value = line.split("=", 1)
                result[name] = value
            i += 1
        return result

    def summary(self) -> str:
        return self.summary_file.read_text()

    def run_script(self, module, *argv: str) -> int:
        with mock.patch.object(sys, "argv", [str(module.__file__), *argv]):
            return module.main()


def stack(status: str = "UPDATE_COMPLETE", **extra) -> dict:
    data = {"StackName": "demo-stack", "StackId": STACK_ID, "StackStatus": status, "CreationTime": T0,
            "EnableTerminationProtection": False}
    data.update(extra)
    return data


def not_found(**_kw):
    return FakeClientError("ValidationError", "Stack with id demo-stack does not exist")
