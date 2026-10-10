#!/usr/bin/env python3
"""Stage 1 · Preflight.

Validates the run inputs, finds the template + params.json, checks params.json against the
template's Parameters, and maps the role alias to the GitHub secret holding the deploy role.

Run twice by the workflow:
  preflight.py                 -> inputs, files, parameters
  preflight.py --check-secrets -> confirms the deploy-role secret (and optional services/AI secrets) exist
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfn_template as t  # noqa: E402
import common as c  # noqa: E402

STACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d$")
TEMPLATE_RE = re.compile(r".*-template\.ya?ml$", re.IGNORECASE)
ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$")
MAX_BODY_BYTES = 51_200        # larger templates must go through S3
MAX_TEMPLATE_BYTES = 1_048_576  # CloudFormation hard limit (S3)
CONFIG = Path(__file__).resolve().parent.parent / "config" / "accounts.json"


class PreflightError(Exception):
    pass


def load_accounts() -> dict:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    return data.get("aliases", data)


def resolve_folder(base: Path, folder: str) -> Path:
    cleaned = folder.strip().strip("/")
    if not cleaned:
        raise PreflightError("folderPath is empty.")
    if cleaned.startswith(("cloudformation/", base.name + "/")):
        cleaned = cleaned.split("/", 1)[1]  # tolerate people pasting the full path
    if ".." in Path(cleaned).parts or Path(cleaned).is_absolute():
        raise PreflightError("folderPath must be a relative path inside cloudformation/ (no '..').")
    path = (base / cleaned).resolve()
    if base.resolve() not in path.parents and path != base.resolve():
        raise PreflightError("folderPath escapes the cloudformation/ directory.")
    if not path.is_dir():
        raise PreflightError(f"Folder '{base.name}/{cleaned}' does not exist in this commit.")
    return path


def locate_template(folder: Path) -> Path:
    top = sorted(p for p in folder.iterdir() if p.is_file() and TEMPLATE_RE.match(p.name))
    if len(top) == 1:
        return top[0]
    if len(top) > 1:
        raise PreflightError(f"More than one *-template.yaml in {folder.name}: {[p.name for p in top]}. "
                             "Keep exactly one template per folder.")
    nested = sorted(p for p in folder.rglob("*") if p.is_file() and TEMPLATE_RE.match(p.name))
    if len(nested) == 1:
        return nested[0]
    if not nested:
        raise PreflightError(f"No file matching '*-template.yaml' / '*-template.yml' in {folder}.")
    raise PreflightError("Several templates found in sub-folders: "
                         f"{[str(p.relative_to(folder)) for p in nested]}. Point folderPath at the exact folder.")


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def run_inputs() -> int:
    stack = c.env("STACK_NAME")
    region = c.env("TARGET_REGION", "eu-west-1")
    alias = c.env("ROLE_ALIAS")
    folder_in = c.env("FOLDER_PATH")
    drift_policy = c.env("DRIFT_POLICY", "block")
    execution_mode = c.env("EXECUTION_MODE", "manual-console")
    base = Path(c.env("CFN_BASE_DIR", "cloudformation"))

    problems: list[str] = []
    warnings: list[str] = []
    if not STACK_RE.match(stack):
        problems.append(f"stackName '{stack}' is invalid: start with a letter, then letters/digits/hyphens, max 128.")
    if not REGION_RE.match(region):
        problems.append(f"AWS_REGION '{region}' does not look like an AWS region (e.g. eu-west-1).")
    accounts = load_accounts()
    account = accounts.get(alias)
    if not account:
        problems.append(f"roleAlias '{alias}' is not in .github/cfn-pipeline/config/accounts.json.")

    template_path = params_path = None
    template_info: dict = {}
    params: list[dict] = []
    template_bytes = 0
    try:
        folder = resolve_folder(base, folder_in)
        template_path = locate_template(folder)
        params_path = folder / "params.json"
        if not params_path.is_file():
            raise PreflightError(f"params.json not found in {rel(folder)}.")
        template_bytes = template_path.stat().st_size
        template_text = template_path.read_text(encoding="utf-8")
        template_info = t.describe_template(t.load_template_text(template_text))
        params = t.load_params(params_path)
        p_errors, p_warnings = t.check_params(params, template_info)
        problems += p_errors
        warnings += p_warnings
    except (PreflightError, t.TemplateError, OSError, UnicodeDecodeError) as exc:
        problems.append(str(exc))

    if template_bytes > MAX_TEMPLATE_BYTES:
        problems.append(f"Template is {template_bytes:,} bytes; CloudFormation's limit is 1 MB.")
    if template_info:
        if template_info["resource_count"] == 0:
            problems.append("Template has no Resources.")
        if template_info["resource_count"] > 500:
            problems.append(f"Template has {template_info['resource_count']} resources (limit 500).")
        elif template_info["resource_count"] > 450:
            warnings.append(f"Template has {template_info['resource_count']} resources — close to the 500 limit.")
        if len(template_info["parameters"]) > 200:
            problems.append("Template declares more than 200 parameters.")
        if template_info["output_count"] > 200:
            problems.append("Template declares more than 200 outputs.")
        for name, value in t.no_echo_values(params, template_info).items():
            c.mask(value)

    large = template_bytes > MAX_BODY_BYTES

    for w in warnings:
        c.warning(w, title="Preflight")
    rows = [
        ["Stack", f"`{stack}`"], ["Account alias", f"`{alias}` ({(account or {}).get('tier', '?')})"],
        ["Region", f"`{region}`"], ["Drift policy", f"`{drift_policy}`"], ["Execution", f"`{execution_mode}`"],
        ["Template", f"`{rel(template_path)}`" if template_path else "—"],
        ["Params", f"`{rel(params_path)}`" if params_path else "—"],
        ["Template size", f"{template_bytes:,} bytes" + (" → staged in S3 (over 51,200)" if large else "")],
    ]
    if template_info:
        rows += [["Resources", template_info["resource_count"]],
                 ["Nested stacks", ", ".join(template_info["nested_stacks"]) or "none"],
                 ["Transform", template_info["transform"] or "none"]]
    summary = ["## 🧭 Preflight", c.md_table(["Check", "Value"], rows)]
    if params and template_info:
        shown = t.display_params(params, template_info)
        summary.append("<details><summary>Parameters from params.json</summary>\n\n"
                       + c.md_table(["Key", "Value"], [[p["ParameterKey"], f"`{p['ParameterValue']}`"] for p in shown])
                       + "\n</details>")
    if warnings:
        summary.append("**Warnings**\n" + "\n".join(f"- ⚠️ {w}" for w in warnings))
    if problems:
        summary.append("**Problems (fix before re-running)**\n" + "\n".join(f"- ❌ {p}" for p in problems))
    c.add_summary("\n\n".join(summary))

    if problems:
        for p in problems:
            c.error(p, title="Preflight failed")
        return 1

    c.set_output("role_secret", account["secret"])
    c.set_output("tier", account.get("tier", "unknown"))
    c.set_output("template_file", rel(template_path))
    c.set_output("params_file", rel(params_path))
    c.set_output("template_bytes", template_bytes)
    c.set_output("large_template", large)
    c.set_output("has_nested_stacks", bool(template_info["nested_stacks"]))
    c.notice(f"{rel(template_path)} → {stack} in {alias}/{region}", title="Preflight passed")
    return 0


def run_secret_check() -> int:
    secret_name = c.env("ROLE_SECRET_NAME")
    role_arn = c.env("ROLE_ARN")
    services_role = c.env("SERVICES_ROLE_ARN")
    has_kiro = c.env_bool("HAS_KIRO_KEY")
    channel = c.env("NOTIFY_CHANNEL", "ses").lower()
    provider = c.env("AI_PROVIDER", "auto").lower()

    if not role_arn:
        c.error(f"Repository secret '{secret_name}' is empty or missing — add the deploy role ARN.",
                title="Deploy role not configured")
        return 1
    if not ARN_RE.match(role_arn):
        c.warning(f"Secret '{secret_name}' does not look like an IAM role ARN.", title="Deploy role")

    services_ready = bool(services_role)
    if channel != "none" and not services_ready:
        c.warning("Emails are switched on but secret CFN_PIPELINE_SERVICES_ROLE is not set — the pipeline will "
                  "still run and write job summaries, but no email will be sent.", title="Notifications")
    if channel == "ses" and services_ready and (not c.env("MAIL_FROM") or not c.env("MAIL_TO")):
        c.warning("Set repository variables CFN_MAIL_FROM and CFN_MAIL_TO to receive emails.", title="Notifications")

    if provider == "none":
        ai = "none"
    elif provider == "kiro":
        ai = "kiro" if has_kiro else "none"
    elif provider == "bedrock":
        ai = "bedrock" if services_ready else "none"
    else:
        ai = "kiro" if has_kiro else ("bedrock" if services_ready else "none")
    if ai == "none" and provider != "none":
        c.warning("Amazon Q / Bedrock drift suggestions are not configured (needs secret KIRO_API_KEY or "
                  "CFN_PIPELINE_SERVICES_ROLE). Drift reports will still include the live YAML.", title="Amazon Q")

    c.set_output("services_role_configured", services_ready)
    c.set_output("ai_ready", ai)
    c.add_summary(c.md_table(["Integration", "Status"], [
        ["Deploy role secret", f"✅ `{secret_name}`"],
        ["Email channel", f"`{channel}` · {'✅ role set' if services_ready else '⚠️ CFN_PIPELINE_SERVICES_ROLE missing'}"
         if channel != "none" else "off"],
        ["Drift fix suggestions", {"kiro": "✅ Amazon Q (Kiro CLI, headless)", "bedrock": "✅ Amazon Bedrock",
                                   "none": "⚪ off — live YAML only"}[ai]],
    ]))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-secrets", action="store_true")
    args = parser.parse_args()
    return run_secret_check() if args.check_secrets else run_inputs()


if __name__ == "__main__":
    sys.exit(main())
