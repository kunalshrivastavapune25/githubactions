"""CloudFormation API operations shared by the drift, change-set and execute stages."""
from __future__ import annotations

import datetime as _dt
import json
import time
from typing import Any

import common as c

UPDATABLE = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
             "IMPORT_COMPLETE", "IMPORT_ROLLBACK_COMPLETE"}
SUCCESS_TERMINAL = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"}
FAILURE_TERMINAL = {"CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "UPDATE_ROLLBACK_COMPLETE",
                    "UPDATE_ROLLBACK_FAILED", "UPDATE_FAILED", "DELETE_COMPLETE", "DELETE_FAILED",
                    "IMPORT_ROLLBACK_COMPLETE", "IMPORT_ROLLBACK_FAILED"}


def describe_stack(cfn: Any, stack: str) -> dict | None:
    """Return the stack description, or None when the stack does not exist."""
    try:
        return cfn.describe_stacks(StackName=stack)["Stacks"][0]
    except Exception as exc:  # botocore ClientError
        if c.error_code(exc) == "ValidationError" and "does not exist" in c.error_message(exc):
            return None
        raise


def is_new_stack(stack: dict | None) -> bool:
    return stack is None or stack.get("StackStatus") == "REVIEW_IN_PROGRESS"


def stack_state_problem(stack_name: str, status: str) -> str | None:
    """Explain why a stack in this status cannot be updated (None when it can)."""
    if status in UPDATABLE or status == "REVIEW_IN_PROGRESS":
        return None
    if status.endswith("_IN_PROGRESS"):
        return (f"Another operation is still running on the stack ({status}). "
                "Wait for it to finish, then re-run the pipeline.")
    if status in {"ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "CREATE_FAILED"}:
        return (f"The stack never finished creating ({status}); CloudFormation only allows deleting it. "
                f"Delete it with `aws cloudformation delete-stack --stack-name {stack_name}` and re-run "
                "the pipeline to create it fresh.")
    if status == "UPDATE_ROLLBACK_FAILED":
        return ("The last update failed to roll back. Recover with "
                f"`aws cloudformation continue-update-rollback --stack-name {stack_name}` "
                "(add `--resources-to-skip` for resources that cannot roll back), then re-run.")
    if status == "UPDATE_FAILED":
        return ("The last update failed with rollback disabled. Roll back with "
                f"`aws cloudformation rollback-stack --stack-name {stack_name}` or fix forward, then re-run.")
    if status in {"DELETE_FAILED", "IMPORT_ROLLBACK_FAILED"}:
        return f"The stack is in {status}. Resolve the failed operation in the console before deploying."
    return f"The stack is in an unexpected state ({status})."


def list_stack_resources(cfn: Any, stack: str) -> list[dict]:
    return list(c.paginate(cfn.list_stack_resources, "StackResourceSummaries", StackName=stack))


def nested_stacks(cfn: Any, stack_id: str, label: str, depth: int = 0, max_depth: int = 5) -> list[dict]:
    """Nested stacks below a stack (drift detection does not cover them automatically)."""
    found: list[dict] = []
    if depth >= max_depth:
        return found
    for res in list_stack_resources(cfn, stack_id):
        if (res.get("ResourceType") == "AWS::CloudFormation::Stack" and res.get("PhysicalResourceId")
                and not str(res.get("ResourceStatus", "")).startswith("DELETE")):
            child_label = f"{label}/{res['LogicalResourceId']}"
            found.append({"label": child_label, "stack_id": res["PhysicalResourceId"], "is_root": False})
            found.extend(nested_stacks(cfn, res["PhysicalResourceId"], child_label, depth + 1, max_depth))
    return found


def run_drift_detection(cfn: Any, targets: list[dict], timeout_s: int = 1200, poll_s: float = 5.0) -> list[dict]:
    """Start drift detection on every target stack, then poll until all finish."""
    results: list[dict] = []
    for target in targets:
        entry = dict(target)
        try:
            entry["detection_id"] = cfn.detect_stack_drift(StackName=target["stack_id"])["StackDriftDetectionId"]
            entry["detection_status"] = "DETECTION_IN_PROGRESS"
        except Exception as exc:
            entry["detection_id"] = None
            entry["detection_status"] = "START_FAILED"
            entry["reason"] = c.error_message(exc)
        results.append(entry)

    deadline = time.time() + timeout_s
    pending = [r for r in results if r["detection_id"]]
    while pending:
        if time.time() > deadline:
            for r in pending:
                r["detection_status"] = "TIMED_OUT"
                r["reason"] = f"Drift detection did not finish within {timeout_s}s."
            break
        time.sleep(poll_s)
        for r in list(pending):
            try:
                status = cfn.describe_stack_drift_detection_status(StackDriftDetectionId=r["detection_id"])
            except Exception as exc:  # treat as inconclusive rather than failing the whole stage
                r.update(detection_status="STATUS_ERROR", reason=c.error_message(exc))
                pending.remove(r)
                continue
            if status.get("DetectionStatus") == "DETECTION_IN_PROGRESS":
                continue
            r.update(
                detection_status=status.get("DetectionStatus"),
                stack_drift_status=status.get("StackDriftStatus"),
                reason=status.get("DetectionStatusReason"),
                drifted_count=status.get("DriftedStackResourceCount", 0),
                detected_at=status.get("Timestamp"),
            )
            pending.remove(r)
    return results


def resource_drifts(cfn: Any, stack_id: str) -> list[dict]:
    return list(c.paginate(cfn.describe_stack_resource_drifts, "StackResourceDrifts",
                           StackName=stack_id, MaxResults=100))


def operation_events(cfn: Any, *, change_set: str | None = None, operation_id: str | None = None,
                     stack: str | None = None, failed_only: bool = False) -> list[dict]:
    """DescribeEvents (early validation / hook / provisioning errors). Empty if unsupported."""
    if not hasattr(cfn, "describe_events"):
        return []
    args: dict[str, Any] = {}
    if change_set:
        args["ChangeSetName"] = change_set
    if operation_id:
        args["OperationId"] = operation_id
    if stack and not (change_set or "").startswith("arn:"):
        args["StackName"] = stack
    if failed_only:
        args["Filters"] = {"FailedEvents": True}
    try:
        events = list(c.paginate(cfn.describe_events, "OperationEvents", **args))
    except Exception as exc:
        return [{"type": "LOOKUP_ERROR", "reason": c.error_message(exc)}]
    keep = {"VALIDATION_ERROR", "HOOK_INVOCATION_ERROR", "PROVISIONING_ERROR"}
    out = []
    for e in events:
        if e.get("EventType") not in keep:
            continue
        out.append({
            "type": e.get("EventType"),
            "validation": e.get("ValidationName"),
            "status": e.get("ValidationStatus") or e.get("HookStatus") or e.get("ResourceStatus"),
            "mode": e.get("ValidationFailureMode") or e.get("HookFailureMode"),
            "path": e.get("ValidationPath"),
            "logical_id": e.get("LogicalResourceId"),
            "resource_type": e.get("ResourceType"),
            "hook": e.get("HookType"),
            "reason": (e.get("ValidationStatusReason") or e.get("HookStatusReason")
                       or e.get("ResourceStatusReason") or ""),
        })
    return out


def wait_for_change_set(cfn: Any, change_set_id: str, timeout_s: int = 1800, poll_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    while True:
        cs = cfn.describe_change_set(ChangeSetName=change_set_id)
        if cs.get("Status") in {"CREATE_COMPLETE", "FAILED", "DELETE_COMPLETE", "DELETE_FAILED"}:
            return cs
        if time.time() > deadline:
            raise TimeoutError(f"Change set still {cs.get('Status')} after {timeout_s}s")
        time.sleep(poll_s)


def describe_change_set_full(cfn: Any, change_set_id: str, include_values: bool = True,
                             depth: int = 0, max_depth: int = 3) -> dict:
    """All pages of a change set (with before/after property values) plus nested change sets."""
    first: dict | None = None
    changes: list[dict] = []
    token = None
    while True:
        args: dict[str, Any] = {"ChangeSetName": change_set_id}
        if include_values:
            args["IncludePropertyValues"] = True
        if token:
            args["NextToken"] = token
        try:
            page = cfn.describe_change_set(**args)
        except Exception as exc:
            if include_values and type(exc).__name__ == "ParamValidationError":
                include_values = False  # very old SDK: fall back to the basic view
                continue
            raise
        first = first or page
        changes.extend(page.get("Changes") or [])
        token = page.get("NextToken")
        if not token:
            break
    for change in changes:
        rc = change.get("ResourceChange") or {}
        if rc.get("ResourceType") == "AWS::CloudFormation::Stack" and rc.get("ChangeSetId") and depth < max_depth:
            try:
                rc["NestedChangeSet"] = describe_change_set_full(cfn, rc["ChangeSetId"], include_values,
                                                                 depth + 1, max_depth)
            except Exception as exc:
                rc["NestedChangeSetError"] = c.error_message(exc)
    result = dict(first or {})
    result["Changes"] = changes
    result.pop("NextToken", None)
    return result


def to_aware(ts: Any) -> _dt.datetime | None:
    if isinstance(ts, _dt.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)
    if isinstance(ts, str):
        try:
            parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return None
    return None


def new_stack_events(cfn: Any, stack_id: str, since: _dt.datetime, seen: set[str]) -> list[dict]:
    """Events newer than `since` that we have not printed yet, oldest first."""
    fresh: list[dict] = []
    for event in c.paginate(cfn.describe_stack_events, "StackEvents", StackName=stack_id):
        ts = to_aware(event.get("Timestamp"))
        if event.get("EventId") in seen or (ts and ts < since):
            break
        fresh.append(event)
    for event in fresh:
        seen.add(event.get("EventId"))
    return list(reversed(fresh))


def format_event(event: dict) -> str:
    reason = event.get("ResourceStatusReason") or ""
    return (f"{c.iso(event.get('Timestamp'))}  {str(event.get('LogicalResourceId', ''))[:40]:40}  "
            f"{str(event.get('ResourceType', ''))[:38]:38}  {str(event.get('ResourceStatus', '')):28}  {reason}")


def failed_events(events: list[dict]) -> list[dict]:
    return [e for e in events if str(e.get("ResourceStatus", "")).endswith("_FAILED")]


def root_cause(failures: list[dict]) -> dict | None:
    """First failure that is not just a cascade ('Resource update cancelled', etc.)."""
    noise = ("cancelled", "canceled", "The following resource(s) failed")
    for f in sorted(failures, key=lambda e: c.iso(e.get("Timestamp"))):
        reason = str(f.get("ResourceStatusReason") or f.get("reason") or "")
        if reason and not any(n in reason for n in noise):
            return f
    return failures[0] if failures else None


def stack_outputs(stack: dict | None) -> list[dict]:
    return [{"key": o.get("OutputKey"), "value": o.get("OutputValue"), "description": o.get("Description", ""),
             "export": o.get("ExportName", "")} for o in (stack or {}).get("Outputs") or []]


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=c.json_default)
