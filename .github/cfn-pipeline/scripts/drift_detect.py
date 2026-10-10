#!/usr/bin/env python3
"""Stage 3 · Drift detection (root stack + every nested stack).

Gate decision (job output `gate`):
  pass  -> stack is new, in sync, or driftPolicy=revert-drift
  block -> drift found, detection inconclusive, or the stack is in a state that cannot be updated

For each drifted resource the report captures what Amazon Q needs to suggest a fix:
the property diff, the live properties as YAML, the resource's code from the repo template,
and (when Cloud Control supports the type) the full live configuration.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfn_ops as ops  # noqa: E402
import cfn_template as t  # noqa: E402
import common as c  # noqa: E402

_SCHEMA_CACHE: dict[str, list[list[str]]] = {}


# ---------------------------------------------------------------- live state helpers

def _read_only_paths(cfn, type_name: str) -> list[list[str]]:
    if type_name not in _SCHEMA_CACHE:
        paths: list[list[str]] = []
        try:
            schema = json.loads(cfn.describe_type(Type="RESOURCE", TypeName=type_name)["Schema"])
            for pointer in schema.get("readOnlyProperties", []):
                if pointer.startswith("/properties/"):
                    paths.append(pointer.split("/")[2:])
        except Exception:  # schema lookup is best effort
            pass
        _SCHEMA_CACHE[type_name] = paths
    return _SCHEMA_CACHE[type_name]


def _delete_path(obj, parts: list[str]) -> None:
    if not parts:
        return
    head, rest = parts[0], parts[1:]
    if isinstance(obj, list):
        items = obj if head == "*" else ([obj[int(head)]] if head.isdigit() and int(head) < len(obj) else [])
        for item in items:
            _delete_path(item, rest)
    elif isinstance(obj, dict) and head in obj:
        if rest:
            _delete_path(obj[head], rest)
        else:
            obj.pop(head, None)


def live_state(cc, cfn, type_name: str, identifier: str, max_chars: int = 40_000) -> dict:
    """Full live configuration via Cloud Control API, minus read-only attributes."""
    try:
        response = cc.get_resource(TypeName=type_name, Identifier=identifier)
        props = json.loads(response["ResourceDescription"]["Properties"])
    except Exception as exc:
        return {"available": False, "reason": c.short(c.error_message(exc), 300)}
    for parts in _read_only_paths(cfn, type_name):
        _delete_path(props, parts)
    text = c.to_yaml(c.redact(props))
    return {"available": True, "yaml": text[:max_chars], "truncated": len(text) > max_chars}


def _diff(p: dict) -> dict:
    path = p.get("PropertyPath") or ""
    expected, actual = c.maybe_json(p.get("ExpectedValue")), c.maybe_json(p.get("ActualValue"))
    last = path.rstrip("/").split("/")[-1] if path else ""
    if last and c.is_sensitive_key(last):
        expected = c.REDACTED if expected is not None else None
        actual = c.REDACTED if actual is not None else None
    return {"path": path, "expected": c.redact(expected), "actual": c.redact(actual),
            "type": p.get("DifferenceType")}


def build_entry(drift: dict, stack_row: dict, region: str, template_text: str | None,
                cfn, cc, fetch_live: bool) -> dict:
    logical_id = drift.get("LogicalResourceId", "?")
    rtype = drift.get("ResourceType", "?")
    status = drift.get("StackResourceDriftStatus")
    actual = c.maybe_json(drift.get("ActualProperties")) if drift.get("ActualProperties") else None
    expected = c.maybe_json(drift.get("ExpectedProperties")) if drift.get("ExpectedProperties") else None
    actual = c.redact(actual) if isinstance(actual, (dict, list)) else actual
    entry = {
        "stack": stack_row["label"],
        "stack_id": stack_row["stack_id"],
        "is_root_stack": stack_row["is_root"],
        "logical_id": logical_id,
        "resource_type": rtype,
        "physical_id": drift.get("PhysicalResourceId"),
        "status": status,
        "detected_at": drift.get("Timestamp"),
        "differences": [_diff(p) for p in drift.get("PropertyDifferences") or []],
        "expected_properties": c.redact(expected) if isinstance(expected, (dict, list)) else expected,
        "actual_properties": actual,
        "live_yaml": c.to_yaml({logical_id: {"Type": rtype, "Properties": actual}}) if isinstance(actual, dict) else None,
        "template_snippet": t.resource_snippet(template_text, logical_id)
        if (stack_row["is_root"] and template_text) else None,
        "console_url": c.drift_console_url(region, stack_row["stack_id"]),
    }
    if fetch_live and status == "MODIFIED" and entry["physical_id"]:
        entry["live_state"] = live_state(cc, cfn, rtype, entry["physical_id"])
    return entry


# ---------------------------------------------------------------- evaluation

def evaluate(cfn, cc, stack_name: str, region: str, policy: str, template_text: str | None) -> dict:
    stack = ops.describe_stack(cfn, stack_name)
    if ops.is_new_stack(stack):
        return {"stack_exists": False, "stack_status": (stack or {}).get("StackStatus", "NOT_FOUND"),
                "stack_id": (stack or {}).get("StackId", ""), "change_set_type": "CREATE",
                "drift_status": "NEW_STACK", "gate": "pass", "block_reason": "", "revert_drift": False,
                "stacks": [], "drifted_resources": [], "counts": {},
                "message": "Stack does not exist yet — nothing to drift. A CREATE change set will be prepared."}

    status, stack_id = stack["StackStatus"], stack["StackId"]
    base = {"stack_exists": True, "stack_status": status, "stack_id": stack_id,
            "account_id": c.account_from_arn(stack_id), "change_set_type": "UPDATE",
            "last_updated": stack.get("LastUpdatedTime") or stack.get("CreationTime"),
            "termination_protection": bool(stack.get("EnableTerminationProtection")),
            "stack_console_url": c.stack_console_url(region, stack_id),
            "drift_console_url": c.drift_console_url(region, stack_id)}
    problem = ops.stack_state_problem(stack_name, status)
    if problem:
        return {**base, "drift_status": "NOT_CHECKED", "gate": "block", "block_reason": "STACK_STATE",
                "revert_drift": False, "message": problem, "stacks": [], "drifted_resources": [], "counts": {}}

    targets = [{"label": stack_name, "stack_id": stack_id, "is_root": True}]
    with c.group("Discovering nested stacks"):
        targets += ops.nested_stacks(cfn, stack_id, stack_name)
        print(f"Stacks to check: {[x['label'] for x in targets]}")

    with c.group("Running drift detection"):
        results = ops.run_drift_detection(cfn, targets, c.env_int("DRIFT_TIMEOUT_SECONDS", 1200),
                                          c.env_float("CFN_POLL_SECONDS", 5))
        for r in results:
            print(f"{r['label']}: {r.get('detection_status')} / {r.get('stack_drift_status')} {r.get('reason') or ''}")

    fetch_live = c.env_bool("CFN_FETCH_LIVE_STATE", True)
    totals: Counter = Counter()
    rows, drifted = [], []
    for r in results:
        row = {k: r.get(k) for k in ("label", "stack_id", "is_root", "detection_status", "stack_drift_status",
                                       "reason", "detected_at", "drifted_count")}
        if r.get("detection_status") in {"DETECTION_COMPLETE", "DETECTION_FAILED"}:
            drifts = ops.resource_drifts(cfn, r["stack_id"])
            counts = Counter(d.get("StackResourceDriftStatus") for d in drifts)
            row["counts"] = dict(counts)
            totals.update(counts)
            for d in drifts:
                if d.get("StackResourceDriftStatus") in {"MODIFIED", "DELETED"}:
                    drifted.append(build_entry(d, row, region, template_text, cfn, cc, fetch_live))
        rows.append(row)

    inconclusive = [r for r in rows if r.get("detection_status") != "DETECTION_COMPLETE"]
    if drifted:
        drift_status = "DRIFTED"
    elif inconclusive:
        drift_status = "UNKNOWN"
    else:
        drift_status = "IN_SYNC"

    if drift_status == "IN_SYNC":
        gate, reason, revert, message = "pass", "", False, "All resources match the template."
    elif policy == "revert-drift":
        gate, reason, revert = "pass", "", True
        message = (f"{len(drifted)} drifted resource(s) — continuing with a drift-aware change set (REVERT_DRIFT) "
                   "that puts live resources back to the template values once executed." if drifted else
                   "Drift check was inconclusive — continuing with a drift-aware change set (REVERT_DRIFT), which "
                   "compares live and template values itself before anything is executed.")
    else:
        gate = "block"
        reason = "DRIFT" if drifted else "INCONCLUSIVE"
        revert = False
        message = (f"{len(drifted)} resource(s) changed outside CloudFormation. Deployment halted."
                   if drifted else "Drift detection could not check every stack/resource. Deployment halted.")
    return {**base, "drift_status": drift_status, "gate": gate, "block_reason": reason, "revert_drift": revert,
            "message": message, "stacks": rows, "drifted_resources": drifted, "counts": dict(totals),
            "inconclusive": [{"label": r["label"], "status": r.get("detection_status"), "reason": r.get("reason")}
                             for r in inconclusive]}


# ---------------------------------------------------------------- reporting

def summary_markdown(report: dict) -> str:
    icon = {"IN_SYNC": "✅", "NEW_STACK": "🆕", "DRIFTED": "❌" if report["gate"] == "block" else "♻️",
            "UNKNOWN": "⚠️", "NOT_CHECKED": "⛔", "ERROR": "💥"}.get(report["drift_status"], "")
    md = [f"## 🔍 Drift detection — {icon} {report['drift_status']}",
          f"**Stack** `{report['stack_name']}` · **Region** `{report['region']}` · **Policy** `{report['drift_policy']}`"
          f" · **Gate** `{report['gate']}`", report.get("message", "")]
    if report.get("stacks"):
        md.append(c.md_table(["Stack", "Detection", "Drift status", "Modified", "Deleted", "In sync"], [
            [s["label"], s.get("detection_status"), s.get("stack_drift_status") or "—",
             (s.get("counts") or {}).get("MODIFIED", 0), (s.get("counts") or {}).get("DELETED", 0),
             (s.get("counts") or {}).get("IN_SYNC", 0)] for s in report["stacks"]]))
    drifted = report.get("drifted_resources") or []
    if drifted:
        md.append(c.md_table(["Resource", "Type", "Status", "Changed properties"], [
            [f"`{d['logical_id']}`" + ("" if d["is_root_stack"] else f" ({d['stack']})"), d["resource_type"],
             d["status"], ", ".join(x["path"] for x in d["differences"][:6]) or "—"] for d in drifted]))
        for d in drifted[:25]:
            block = [f"<details><summary><b>{d['logical_id']}</b> · {d['resource_type']} · {d['status']}</summary>\n"]
            if d["differences"]:
                block.append(c.md_table(["Property", "Template (expected)", "Live (actual)", "Difference"], [
                    [f"`{x['path']}`", c.short(x["expected"], 120), c.short(x["actual"], 120), x["type"]]
                    for x in d["differences"]]))
            if d.get("live_yaml"):
                block.append("\n**Live YAML (drift-tracked properties)**\n\n```yaml\n" + d["live_yaml"].rstrip() + "\n```")
            block.append("\n</details>")
            md.append("\n".join(block))
    if report.get("error"):
        md.append(f"```\n{report['error']}\n```")
    return "\n\n".join(m for m in md if m)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/drift-report.json")
    args = ap.parse_args()

    stack_name = c.env("STACK_NAME")
    region = c.env("TARGET_REGION", "eu-west-1")
    policy = c.env("DRIFT_POLICY", "block")
    template_file = c.env("TEMPLATE_FILE")
    template_text = None
    if template_file and Path(template_file).is_file():
        template_text = Path(template_file).read_text(encoding="utf-8")

    report = {"stack_name": stack_name, "region": region, "drift_policy": policy, "template_file": template_file,
              "run": c.run_context(), "checked_at": c.utcnow()}
    exit_code = 0
    try:
        cfn = c.aws_client("cloudformation", region)
        cc = c.aws_client("cloudcontrol", region)
        report.update(evaluate(cfn, cc, stack_name, region, policy, template_text))
    except Exception as exc:  # unexpected API/permission problems
        report.update(gate="block", block_reason="ERROR", drift_status="ERROR", revert_drift=False,
                      change_set_type="", stack_exists="", stack_status="",
                      message="Drift check could not complete.", error=c.error_message(exc))
        c.error(c.error_message(exc), title="Drift detection error")
        exit_code = 1

    c.write_json(args.out, report)
    drifted = report.get("drifted_resources") or []
    for key in ("gate", "block_reason", "drift_status", "change_set_type", "stack_status"):
        c.set_output(key, report.get(key, ""))
    c.set_output("drifted_count", len(drifted))
    c.set_output("stack_exists", report.get("stack_exists", ""))
    c.set_output("revert_drift", bool(report.get("revert_drift")))

    names = ", ".join(f"{d['logical_id']} ({d['status']})" for d in drifted[:10])
    if report["drift_status"] == "DRIFTED" and report["gate"] == "block":
        c.error(f"{len(drifted)} drifted resource(s): {names}. Pipeline will stop and email the report.",
                title="Drift detected")
    elif report["drift_status"] == "DRIFTED":
        c.warning(f"{len(drifted)} drifted resource(s) will be reverted by a drift-aware change set: {names}",
                  title="Drift will be reverted")
    elif report.get("block_reason") in {"STACK_STATE", "INCONCLUSIVE"}:
        c.error(report.get("message", ""), title="Deployment blocked")
    elif exit_code == 0:
        c.notice(report.get("message", ""), title=f"Drift: {report['drift_status']}")
    c.add_summary(summary_markdown(report))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
