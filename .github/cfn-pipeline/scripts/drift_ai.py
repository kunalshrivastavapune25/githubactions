#!/usr/bin/env python3
"""Stage 4 · Amazon Q drift fix suggestions.

For every drifted resource, ask an AI assistant to turn the live configuration into a
ready-to-paste CloudFormation snippet that keeps the template's style (!Ref, !Sub, params).

Providers (CFN_AI_PROVIDER = auto | kiro | bedrock | none):
  kiro    — Amazon Q Developer CLI, now Kiro CLI, in headless mode (needs secret KIRO_API_KEY,
            a Kiro / Q Developer Pro subscription and API keys enabled by your admin).
  bedrock — Amazon Bedrock Converse API using the pipeline services role (no extra subscription).
  auto    — Kiro when its key is present, otherwise Bedrock; falls back between them per resource.

This stage is advisory: it never fails the pipeline. The drift email always carries the
deterministic live YAML even if no provider is available.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfn_template as t  # noqa: E402
import common as c  # noqa: E402

MARK_BEGIN, MARK_END = "BEGIN_DRIFT_FIX_JSON", "END_DRIFT_FIX_JSON"
ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
RECOMMENDATIONS = {"ADOPT_LIVE", "REVERT_TO_TEMPLATE", "NEEDS_REVIEW"}
RISKS = {"LOW", "MEDIUM", "HIGH"}

SYSTEM_PROMPT = (
    "You are an expert AWS CloudFormation engineer helping a platform team handle configuration drift. "
    "You write precise, minimal, production-quality CloudFormation YAML, you respect the existing code style, "
    "and you call out changes that weaken security (open ingress, wildcard IAM, disabled encryption or logging, "
    "public access). Answer only in the requested JSON format."
)


# ---------------------------------------------------------------- prompt

def _block(lang: str, body: str) -> str:
    return f"```{lang}\n{body.rstrip()}\n```"


def build_prompt(entry: dict, ctx: dict) -> str:
    snippet = entry.get("template_snippet")
    live_full = (entry.get("live_state") or {}).get("yaml") if (entry.get("live_state") or {}).get("available") else None
    parts = [
        f"Stack `{ctx['stack']}` in {ctx['region']} (account alias {ctx['alias']}, tier {ctx['tier']}) has drifted.",
        f"Resource: `{entry['logical_id']}` ({entry['resource_type']}) — physical id `{entry.get('physical_id')}`.",
        f"Drift status: {entry['status']} (MODIFIED = properties changed outside CloudFormation; "
        "DELETED = resource removed outside CloudFormation).",
        "\n### Property differences (expected = value CloudFormation deployed, actual = live value)",
        _block("json", json.dumps(entry.get("differences") or [], indent=2, default=c.json_default)),
    ]
    if snippet:
        parts += [f"\n### This resource's code in the repository template ({ctx['template_file']})", _block("yaml", snippet)]
    else:
        parts += ["\n### Repository code", "Not available (the resource lives in a nested stack). Build the block from "
                  "the live properties."]
    if ctx.get("parameters_snippet"):
        parts += ["\n### Template Parameters section", _block("yaml", ctx["parameters_snippet"])]
    if ctx.get("params"):
        parts += ["\n### Deployed parameter values from params.json (NoEcho values masked)",
                  _block("json", json.dumps(ctx["params"], indent=2))]
    if entry.get("live_yaml"):
        parts += ["\n### Live properties tracked by drift detection (resolved values — no intrinsic functions)",
                  _block("yaml", entry["live_yaml"])]
    if live_full:
        parts += ["\n### Full live configuration from AWS Cloud Control API (read-only attributes removed; may include "
                  "properties the template does not set — use for context only)", _block("yaml", live_full[:20000])]
    lid = entry["logical_id"]
    parts += [
        "\n## Your task",
        "1. Explain briefly what was changed outside CloudFormation and the likely operational/security impact.",
        "2. Recommend one of: ADOPT_LIVE (change looks intentional and safe — update the template to match live), "
        "REVERT_TO_TEMPLATE (change weakens security/compliance or looks accidental — keep the template and restore the "
        "live resource, e.g. re-run the pipeline with driftPolicy=revert-drift), NEEDS_REVIEW (cannot tell).",
        f"3. Write `updated_resource_yaml`: the complete resource block, starting with `{lid}:` at column 0, that makes "
        "the template match the LIVE value of every drifted property. Keep everything else exactly as in the repository "
        "code: intrinsic functions (!Ref, !Sub, !GetAtt, !If, !FindInMap ...), Condition, DependsOn, DeletionPolicy, "
        "UpdateReplacePolicy, Metadata, property order, comments and 2-space indentation. If a drifted value comes from a "
        "template parameter, keep the !Ref and list the new value in `parameter_changes` instead. For a DELETED resource "
        "return the existing block unchanged and explain the recovery options in `review_notes`.",
        "4. Do not invent properties, do not copy read-only or default-only attributes, and never output secrets.",
        "\nRespond with ONLY this JSON object, between the two marker lines:",
        MARK_BEGIN,
        '{"summary": "<what changed>", "impact": "<impact>", "recommendation": "<ADOPT_LIVE or REVERT_TO_TEMPLATE or '
        'NEEDS_REVIEW>", "risk": "<LOW or MEDIUM or HIGH>", "rationale": "<why>", "updated_resource_yaml": "<YAML block '
        'with \\n line breaks>", "parameter_changes": [{"ParameterKey": "<key>", "NewValue": "<value>", "Reason": "<why>"}], '
        '"review_notes": ["<note>"]}',
        MARK_END,
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- response parsing

def extract_json(text: str) -> dict | None:
    text = ANSI.sub("", text or "")
    candidates: list[str] = []
    begin = text.rfind(MARK_BEGIN)
    if begin >= 0:
        end = text.find(MARK_END, begin)
        candidates.append(text[begin + len(MARK_BEGIN): end if end > 0 else len(text)])
    candidates += list(reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)))
    first, last = text.find("{"), text.rfind("}")
    if 0 <= first < last:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            brace = candidate.find("{")
            candidate = candidate[brace:] if brace >= 0 else candidate
        try:
            data = json.loads(candidate, strict=False)
        except ValueError:
            continue
        if isinstance(data, dict) and _looks_real(data):
            return data
    return None


def _looks_real(data: dict) -> bool:
    """Reject the echoed template ('<what changed>' placeholders)."""
    summary = str(data.get("summary", ""))
    return bool(summary) and not summary.startswith("<") and "updated_resource_yaml" in data


def validate_yaml(text: str, logical_id: str) -> tuple[bool, str]:
    if not text.strip():
        return False, "No code returned."
    try:
        data = t.load_template_text(text)
    except t.TemplateError as exc:
        return False, str(exc)
    block = data.get(logical_id)
    if isinstance(block, dict) and "Type" in block:
        return True, ""
    if "Type" in data:
        return True, f"Returned without the '{logical_id}:' wrapper."
    return False, f"Expected a block starting with '{logical_id}:'."


def normalize(data: dict, logical_id: str) -> dict:
    rec = str(data.get("recommendation", "")).strip().upper()
    risk = str(data.get("risk", "")).strip().upper()
    code = str(data.get("updated_resource_yaml") or "").strip("\n")
    code = re.sub(r"^\s*```(?:ya?ml)?\s*\n|\n\s*```\s*$", "", code)
    valid, note = validate_yaml(code, logical_id)
    params = []
    for p in data.get("parameter_changes") or []:
        if isinstance(p, dict) and p.get("ParameterKey"):
            params.append({"ParameterKey": str(p["ParameterKey"]), "NewValue": str(p.get("NewValue", "")),
                           "Reason": str(p.get("Reason", ""))})
    notes = [str(n) for n in (data.get("review_notes") or []) if str(n).strip()]
    if note:
        notes.append(f"Code check: {note}")
    return {
        "summary": str(data.get("summary", "")).strip(),
        "impact": str(data.get("impact", "")).strip(),
        "recommendation": rec if rec in RECOMMENDATIONS else "NEEDS_REVIEW",
        "risk": risk if risk in RISKS else "MEDIUM",
        "rationale": str(data.get("rationale", "")).strip(),
        "updated_resource_yaml": code,
        "yaml_valid": valid,
        "parameter_changes": params,
        "review_notes": notes,
    }


# ---------------------------------------------------------------- providers

def call_kiro(prompt: str, model: str, timeout: int) -> tuple[str, dict]:
    exe = shutil.which("kiro-cli") or shutil.which("q")
    if not exe:
        raise RuntimeError("kiro-cli is not installed on the runner")
    cmd = [exe, "chat", "--no-interactive", f"--trust-tools={c.env('KIRO_TRUST_TOOLS', 'read,grep')}"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=f"{SYSTEM_PROMPT}\n\n{prompt}", capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"}, check=False)
    out = ANSI.sub("", proc.stdout or "")
    if proc.returncode != 0 and MARK_BEGIN not in out:
        tail = ANSI.sub("", (proc.stderr or "") + "\n" + out).strip()[-600:]
        raise RuntimeError(f"kiro-cli exited with code {proc.returncode}: {tail}")
    return out, {"exit_code": proc.returncode}


def call_bedrock(prompt: str, model_id: str, region: str, max_tokens: int) -> tuple[str, dict]:
    client = c.aws_client("bedrock-runtime", region)
    response = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens},
    )
    blocks = ((response.get("output") or {}).get("message") or {}).get("content") or []
    text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
    usage = response.get("usage") or {}
    return text, {"input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"),
                  "stop_reason": response.get("stopReason")}


def provider_order(requested: str) -> list[str]:
    requested = (requested or "auto").lower()
    kiro_ok = bool(os.environ.get("KIRO_API_KEY")) and bool(shutil.which("kiro-cli") or shutil.which("q"))
    aws_ok = any(os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE"))
    if requested == "none":
        return []
    if requested in {"kiro", "bedrock"}:
        return [requested]
    return [p for p, ok in (("kiro", kiro_ok), ("bedrock", aws_ok)) if ok]


def provider_label(provider: str, model: str) -> str:
    if provider == "kiro":
        return "Amazon Q Developer (Kiro CLI)" + (f" · {model}" if model else "")
    return f"Amazon Bedrock · {model}"


# ---------------------------------------------------------------- output

def markdown_report(report: dict) -> str:
    md = [f"# Suggested drift fixes — {report['stack']} ({report['region']})", ""]
    if not report["suggestions"]:
        md.append("No suggestions generated: " + (report.get("note") or "no provider available."))
    for s in report["suggestions"]:
        md.append(f"## {s['logical_id']} · {s.get('resource_type', '')}")
        if not s.get("ok"):
            md.append(f"_No suggestion: {s.get('error', 'unknown error')}_\n")
            continue
        md += [f"**Recommendation:** {s['recommendation']} · **Risk:** {s['risk']} · _{s['provider_label']}_", "",
               s["summary"], "", f"**Impact:** {s['impact']}", "", f"**Why:** {s['rationale']}", ""]
        if s["updated_resource_yaml"]:
            md += ["**Suggested template code**" + ("" if s["yaml_valid"] else " (⚠️ review — did not parse cleanly)"),
                   "", "```yaml", s["updated_resource_yaml"], "```", ""]
        if s["parameter_changes"]:
            md.append(c.md_table(["Parameter", "New value", "Reason"],
                                 [[p["ParameterKey"], p["NewValue"], p["Reason"]] for p in s["parameter_changes"]]))
        for note in s["review_notes"]:
            md.append(f"- {note}")
        md.append("")
    md.append("> AI-generated suggestion — review before committing. Values come from the live resource.")
    return "\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default="reports/drift-report.json")
    ap.add_argument("--out", default="reports/ai-suggestions.json")
    args = ap.parse_args()

    drift = c.read_json(args.report, {}) or {}
    drifted = drift.get("drifted_resources") or []
    template_file = c.env("TEMPLATE_FILE", drift.get("template_file", ""))
    params_file = c.env("PARAMS_FILE")
    ctx = {"stack": drift.get("stack_name", c.env("STACK_NAME")), "region": drift.get("region", c.env("TARGET_REGION")),
           "alias": c.env("ROLE_ALIAS", "?"), "tier": c.env("TIER", "?"), "template_file": template_file,
           "parameters_snippet": None, "params": None}
    if template_file and Path(template_file).is_file():
        text = Path(template_file).read_text(encoding="utf-8")
        ctx["parameters_snippet"] = t.section_snippet(text, "Parameters")
        if params_file and Path(params_file).is_file():
            try:
                info = t.describe_template(t.load_template_text(text))
                ctx["params"] = t.display_params(t.load_params(params_file), info)
            except t.TemplateError:
                pass

    order = provider_order(c.env("AI_PROVIDER", "auto"))
    bedrock_model = c.env("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-5-5")
    kiro_model = c.env("KIRO_MODEL")
    region = c.env("SERVICES_REGION", "eu-west-1")
    limit = c.env_int("MAX_AI_RESOURCES", 10)
    report = {"stack": ctx["stack"], "region": ctx["region"], "providers": order, "generated_at": c.utcnow(),
              "suggestions": []}
    if not order:
        report["note"] = "No AI provider configured (set KIRO_API_KEY or CFN_PIPELINE_SERVICES_ROLE)."
        c.warning(report["note"], title="Amazon Q")

    unavailable: set[str] = set()  # providers that errored (auth, access, missing CLI) are not retried
    for index, entry in enumerate(drifted if order else []):
        base = {"logical_id": entry["logical_id"], "stack": entry.get("stack"), "resource_type": entry["resource_type"]}
        if index >= limit:
            report["suggestions"].append({**base, "ok": False, "error": f"Skipped — limit of {limit} resources (MAX_AI_RESOURCES)."})
            continue
        prompt = build_prompt(entry, ctx)
        errors, raw = [], ""
        result = None
        for provider in [p for p in order if p not in unavailable]:
            model = kiro_model if provider == "kiro" else bedrock_model
            with c.group(f"{provider_label(provider, model)} → {entry['logical_id']}"):
                try:
                    if provider == "kiro":
                        text, meta = call_kiro(prompt, kiro_model, c.env_int("KIRO_TIMEOUT_SECONDS", 600))
                    else:
                        text, meta = call_bedrock(prompt, bedrock_model, region, c.env_int("BEDROCK_MAX_TOKENS", 8000))
                except Exception as exc:  # provider unavailable / throttled / no access
                    errors.append(f"{provider}: {c.short(c.error_message(exc), 400)}")
                    print(errors[-1])
                    unavailable.add(provider)
                    continue
                parsed = extract_json(text)
                if not parsed:
                    raw = text[-3000:]
                    errors.append(f"{provider}: response did not contain the expected JSON")
                    print(errors[-1])
                    continue
                result = {**base, **normalize(parsed, entry["logical_id"]), "ok": True, "provider": provider,
                          "model": model, "provider_label": provider_label(provider, model), "usage": meta}
                print(f"{result['recommendation']} / risk {result['risk']} / yaml_valid={result['yaml_valid']}")
                break
        if result is None:
            result = {**base, "ok": False, "error": "; ".join(errors) or "no provider", "raw_excerpt": raw}
            c.warning(f"No suggestion for {entry['logical_id']}: {result['error']}", title="Amazon Q")
        report["suggestions"].append(result)

    c.write_json(args.out, report)
    md_path = Path(args.out).with_name("suggested-fixes.md")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    ok = [s for s in report["suggestions"] if s.get("ok")]
    c.set_output("suggestions", len(ok))

    summary = [f"## 🤖 Amazon Q drift fix — {len(ok)}/{len(drifted)} suggestion(s)"]
    if ok:
        summary.append(c.md_table(["Resource", "Recommendation", "Risk", "Provider", "Code"], [
            [f"`{s['logical_id']}`", s["recommendation"], s["risk"], s["provider_label"],
             "✅ parses" if s["yaml_valid"] else "⚠️ review"] for s in ok]))
        for s in ok:
            summary.append(f"<details><summary><b>{s['logical_id']}</b> — suggested code</summary>\n\n{s['summary']}\n\n"
                           f"```yaml\n{s['updated_resource_yaml']}\n```\n</details>")
    elif report.get("note"):
        summary.append(report["note"])
    c.add_summary("\n\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
