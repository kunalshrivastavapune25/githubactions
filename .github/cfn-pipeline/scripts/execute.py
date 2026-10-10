#!/usr/bin/env python3
"""Stage 7b · Execute the approved change set and stream stack events until it finishes.

Before executing it re-checks that the change set is still AVAILABLE and that no new drift
appeared while the run was waiting for approval. If someone already executed the change set
from the console, it switches to "watch and report" mode instead of failing.
"""
from __future__ import annotations

import datetime as _dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfn_ops as ops  # noqa: E402
import common as c  # noqa: E402


class PipelineError(Exception):
    pass


def watch(cfn, stack_id: str, since: _dt.datetime, timeout_s: int, poll_s: float, started: bool) -> tuple[str, list]:
    seen: set[str] = set()
    events: list[dict] = []
    deadline = time.time() + timeout_s
    print(f"{'Time (UTC)':23}  {'Logical ID':40}  {'Type':38}  {'Status':28}  Reason")
    while True:
        fresh = ops.new_stack_events(cfn, stack_id, since, seen)
        for event in fresh:
            print(ops.format_event(event), flush=True)
        events.extend(fresh)
        started = started or bool(fresh)
        stack = ops.describe_stack(cfn, stack_id)
        status = (stack or {}).get("StackStatus", "DELETE_COMPLETE")
        started = started or (status.endswith("_IN_PROGRESS") and status != "REVIEW_IN_PROGRESS")
        if started and (status in ops.SUCCESS_TERMINAL or status in ops.FAILURE_TERMINAL):
            tail = ops.new_stack_events(cfn, stack_id, since, seen)
            for event in tail:
                print(ops.format_event(event), flush=True)
            return status, events + tail
        if time.time() > deadline:
            raise PipelineError(f"Timed out after {timeout_s // 60} min — stack is still {status}. "
                                "Check the stack events in the console.")
        time.sleep(poll_s)


def recheck_drift(cfn, stack_name: str, stack_id: str, poll_s: float) -> list[str]:
    targets = [{"label": stack_name, "stack_id": stack_id, "is_root": True}] + ops.nested_stacks(cfn, stack_id, stack_name)
    drifted: list[str] = []
    for result in ops.run_drift_detection(cfn, targets, c.env_int("DRIFT_TIMEOUT_SECONDS", 1200), poll_s):
        if result.get("detection_status") not in {"DETECTION_COMPLETE", "DETECTION_FAILED"}:
            continue
        for d in ops.resource_drifts(cfn, result["stack_id"]):
            if d.get("StackResourceDriftStatus") in {"MODIFIED", "DELETED"}:
                drifted.append(f"{result['label']}/{d.get('LogicalResourceId')} ({d.get('StackResourceDriftStatus')})")
    return drifted


def run(result: dict) -> None:
    region, cs_arn, stack_name = result["region"], result["change_set_arn"], result["stack_name"]
    poll_s = c.env_float("CFN_POLL_SECONDS", 10)
    cfn = c.aws_client("cloudformation", region)

    cs = cfn.describe_change_set(ChangeSetName=cs_arn)
    stack_id = cs.get("StackId", "")
    result.update(stack_id=stack_id, stack_console_url=c.stack_console_url(region, stack_id),
                  events_console_url=c.events_console_url(region, stack_id),
                  deployment_mode=cs.get("DeploymentMode"))
    c.set_output("stack_console_url", result["stack_console_url"])
    exec_status, status = cs.get("ExecutionStatus"), cs.get("Status")

    elsewhere = exec_status in {"EXECUTE_IN_PROGRESS", "EXECUTE_COMPLETE", "EXECUTE_FAILED"}
    if elsewhere:
        result["already_executed"] = True
        c.notice(f"Change set was already executed ({exec_status}) — from the console/CLI or an earlier attempt. "
                 "Reporting its outcome instead of executing again; start a new run to deploy again.",
                 title="Already executed")
        since = ops.to_aware(cs.get("CreationTime")) or (c.utcnow() - _dt.timedelta(hours=1))
    elif status != "CREATE_COMPLETE" or exec_status != "AVAILABLE":
        raise PipelineError(f"Change set can no longer be executed (Status={status}, ExecutionStatus={exec_status}). "
                            "It was deleted or made OBSOLETE by another update — re-run the pipeline for a fresh one.")
    else:
        stack = ops.describe_stack(cfn, stack_id)
        current = (stack or {}).get("StackStatus", "")
        if current.endswith("_IN_PROGRESS") and current != "REVIEW_IN_PROGRESS":
            raise PipelineError(f"Another operation is running on the stack ({current}). Try again when it finishes.")
        is_update = bool(stack) and current != "REVIEW_IN_PROGRESS"
        if c.env_bool("RECHECK_DRIFT", True) and is_update and cs.get("DeploymentMode") != "REVERT_DRIFT":
            with c.group("Re-checking drift before execution"):
                drifted = recheck_drift(cfn, stack_name, stack_id, c.env_float("CFN_POLL_SECONDS", 5))
            result["drift_recheck"] = drifted or "IN_SYNC"
            if drifted:
                raise PipelineError("New drift appeared while waiting for approval: " + ", ".join(drifted[:10])
                                    + ". Re-run the pipeline so the drift is reviewed before deploying.")
        run_ctx = result["run"]
        token = f"gha-{run_ctx['run_id']}-{run_ctx['run_attempt']}-exec"[:128]
        since = c.utcnow() - _dt.timedelta(seconds=5)
        cfn.execute_change_set(ChangeSetName=cs_arn, ClientRequestToken=token)
        result["client_request_token"] = token
        c.notice(f"Executing {result['change_set_name'] or cs_arn} (approved by {result['approver'] or 'n/a'})",
                 title="Deploying")

    result["started_at"] = c.utcnow()
    with c.group("Stack events"):
        final, events = watch(cfn, stack_id, since, c.env_int("EXECUTE_TIMEOUT_MINUTES", 180) * 60, poll_s,
                              started=exec_status in {"EXECUTE_COMPLETE", "EXECUTE_FAILED"})
    result["finished_at"] = c.utcnow()
    result["duration_seconds"] = int((result["finished_at"] - result["started_at"]).total_seconds())
    result["final_stack_status"] = final
    result["success"] = final in ops.SUCCESS_TERMINAL
    result["status"] = "SUCCEEDED" if result["success"] else "FAILED"
    result["events_count"] = len(events)

    failures = ops.failed_events(events)
    for failure in list(failures):
        if failure.get("ResourceType") == "AWS::CloudFormation::Stack" and failure.get("PhysicalResourceId") \
                and failure.get("PhysicalResourceId") != stack_id:
            try:
                nested = ops.new_stack_events(cfn, failure["PhysicalResourceId"], since, set())
                failures += [dict(e, NestedStack=failure.get("LogicalResourceId")) for e in ops.failed_events(nested)]
            except Exception:  # nested stack may be gone after rollback
                pass
    result["failures"] = [{"logical_id": f.get("LogicalResourceId"), "resource_type": f.get("ResourceType"),
                           "status": f.get("ResourceStatus"), "reason": f.get("ResourceStatusReason", ""),
                           "timestamp": f.get("Timestamp"), "nested_stack": f.get("NestedStack", "")} for f in failures]
    cause = ops.root_cause(failures)
    if cause:
        result["root_cause"] = {"logical_id": cause.get("LogicalResourceId"), "resource_type": cause.get("ResourceType"),
                                "reason": cause.get("ResourceStatusReason", ""), "nested_stack": cause.get("NestedStack", "")}

    after = ops.describe_stack(cfn, stack_id)
    if not result["success"]:
        op_id = (((after or {}).get("LastOperations") or [{}])[0]).get("OperationId")
        if op_id:
            result["operation_failures"] = ops.operation_events(cfn, operation_id=op_id, failed_only=True)
    result["outputs"] = ops.stack_outputs(after) if result["success"] else []


def summary_markdown(result: dict) -> str:
    icon = "✅" if result.get("success") else "❌"
    md = [f"## 🚀 Execute change set — {icon} {result.get('final_stack_status') or result.get('status')}"]
    facts = [["Change set", f"`{result.get('change_set_name')}`"], ["Approved by", result.get("approver") or "—"],
             ["Duration", c.duration(result.get("duration_seconds"))],
             ["Stack", f"[console]({result.get('stack_console_url', '')})"]]
    md.append(c.md_table(["", ""], facts))
    if result.get("error"):
        md.append(f"> {result['error']}")
    if result.get("root_cause"):
        rc = result["root_cause"]
        md.append(f"**Root cause:** `{rc['logical_id']}` ({rc['resource_type']}) — {rc['reason']}")
    if result.get("failures"):
        md.append(c.md_table(["Resource", "Status", "Reason"], [
            [f"`{f['logical_id']}`" + (f" ({f['nested_stack']})" if f["nested_stack"] else ""), f["status"],
             c.short(f["reason"], 200)] for f in result["failures"][:50]]))
    if result.get("outputs"):
        md.append(c.md_table(["Output", "Value"], [[o["key"], c.short(o["value"], 200)] for o in result["outputs"]]))
    return "\n\n".join(md)


def main() -> int:
    result = {"stack_name": c.env("STACK_NAME"), "region": c.env("TARGET_REGION", "eu-west-1"),
              "change_set_arn": c.env("CHANGE_SET_ARN"), "change_set_name": c.env("CHANGE_SET_NAME"),
              "approver": c.env("APPROVER"), "approval_comment": c.env("APPROVAL_COMMENT"), "run": c.run_context(),
              "status": "NOT_STARTED", "success": False, "failures": [], "outputs": []}
    try:
        run(result)
    except Exception as exc:
        result.update(status="ERROR", success=False, error=c.error_message(exc))
        c.error(c.error_message(exc), title="Execution stopped")
    c.write_json(c.env("RESULT_FILE", "reports/execution-result.json"), result)
    c.set_output("status", result.get("final_stack_status") or result["status"])
    c.set_output("success", result["success"])
    for out in result.get("outputs") or []:
        if out.get("key"):
            c.set_output(f"output_{out['key']}", out.get("value", ""))
    if result["success"]:
        c.notice(f"{result['stack_name']} → {result['final_stack_status']}", title="Deployment succeeded")
    elif result.get("root_cause"):
        c.error(f"{result['root_cause']['logical_id']}: {result['root_cause']['reason']}", title="Deployment failed")
    c.add_summary(summary_markdown(result))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
