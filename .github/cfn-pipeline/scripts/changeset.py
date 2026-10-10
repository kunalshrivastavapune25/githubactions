#!/usr/bin/env python3
"""Stage 5 · Create the change set and assess what it will do.

* CREATE change set for new stacks, UPDATE otherwise; drift-aware (REVERT_DRIFT) when the
  drift stage allowed it.
* Waits for CloudFormation's pre-deployment validation; on failure prints the exact
  validation errors (DescribeEvents) instead of a generic "FAILED".
* "No changes" is a clean outcome: the empty change set is deleted and the run stays green.
* Every change gets a risk rating (replacement / deletion of stateful or security-sensitive
  resources, drift reverts, dynamic changes) that drives the approval email.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cfn_ops as ops  # noqa: E402
import cfn_template as t  # noqa: E402
import common as c  # noqa: E402

CAPABILITIES = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
NO_CHANGE_HINTS = ("didn't contain changes", "No updates are to be performed", "does not contain changes")

STATEFUL_TYPES = {
    "AWS::RDS::DBInstance", "AWS::RDS::DBCluster", "AWS::DynamoDB::Table", "AWS::DynamoDB::GlobalTable",
    "AWS::S3::Bucket", "AWS::EFS::FileSystem", "AWS::FSx::FileSystem", "AWS::ElastiCache::ReplicationGroup",
    "AWS::ElastiCache::CacheCluster", "AWS::OpenSearchService::Domain", "AWS::Elasticsearch::Domain",
    "AWS::Redshift::Cluster", "AWS::DocDB::DBCluster", "AWS::Neptune::DBCluster", "AWS::KMS::Key",
    "AWS::SecretsManager::Secret", "AWS::Kinesis::Stream", "AWS::SQS::Queue", "AWS::SNS::Topic",
    "AWS::Logs::LogGroup", "AWS::EC2::Volume", "AWS::Route53::HostedZone", "AWS::Cognito::UserPool",
    "AWS::Backup::BackupVault", "AWS::MSK::Cluster", "AWS::EKS::Cluster", "AWS::ECR::Repository",
    "AWS::DirectoryService::MicrosoftAD", "AWS::DirectoryService::SimpleAD", "AWS::EC2::VPC",
    "AWS::EC2::TransitGateway", "AWS::EC2::Subnet", "AWS::Timestream::Table", "AWS::QLDB::Ledger",
}
SENSITIVE_PREFIXES = (
    "AWS::IAM::", "AWS::KMS::", "AWS::EC2::SecurityGroup", "AWS::EC2::NetworkAcl", "AWS::EC2::Route",
    "AWS::EC2::TransitGateway", "AWS::EC2::VPCPeeringConnection", "AWS::EC2::VPCEndpoint", "AWS::Route53::",
    "AWS::Route53Resolver::", "AWS::S3::BucketPolicy", "AWS::WAFv2::", "AWS::FMS::", "AWS::NetworkFirewall::",
    "AWS::Organizations::", "AWS::SSO::", "AWS::Lambda::Permission", "AWS::SecretsManager::",
    "AWS::CloudTrail::", "AWS::Config::", "AWS::GuardDuty::", "AWS::SecurityHub::", "AWS::RAM::",
)


def assess(row: dict) -> tuple[str, list[str]]:
    rtype, action, repl = row["resource_type"] or "", row["action"], row["replacement"]
    stateful = rtype in STATEFUL_TYPES
    sensitive = rtype.startswith(SENSITIVE_PREFIXES)
    level, reasons = "LOW", []
    if action == "Remove":
        if row.get("policy_action") in {"Retain", "Snapshot"}:
            level = "MEDIUM"
            reasons.append(f"Removed from the stack (DeletionPolicy {row['policy_action']} keeps the resource/snapshot)")
        else:
            level = "HIGH" if (stateful or sensitive) else "MEDIUM"
            reasons.append("Resource will be DELETED")
    if action == "Modify" and repl == "True":
        level = c.max_risk(level, "HIGH" if stateful else "MEDIUM")
        reasons.append("Resource will be REPLACED (new physical resource" + (", data loss risk)" if stateful else ")"))
    if action == "Modify" and repl == "Conditional":
        level = c.max_risk(level, "HIGH" if stateful else "MEDIUM")
        reasons.append("May be replaced, depending on values resolved at deploy time")
    if action == "Dynamic":
        level = c.max_risk(level, "MEDIUM")
        reasons.append("Change is only known at deploy time (dynamic reference / nested value)")
    if action == "Import":
        level = c.max_risk(level, "MEDIUM")
        reasons.append("Existing resource will be imported into the stack")
    if sensitive and action in {"Add", "Modify", "Remove", "SyncWithActual"}:
        level = c.max_risk(level, "MEDIUM")
        reasons.append("Security / network-sensitive resource type")
    if row.get("drift_status") in {"MODIFIED", "DELETED"}:
        level = c.max_risk(level, "MEDIUM")
        reasons.append("Overwrites a change made outside CloudFormation (drift revert)")
    return level, reasons


def normalize(cs: dict, prefix: str = "") -> list[dict]:
    rows: list[dict] = []
    for change in cs.get("Changes") or []:
        rc = change.get("ResourceChange") or {}
        logical = f"{prefix}/{rc.get('LogicalResourceId')}" if prefix else str(rc.get("LogicalResourceId"))
        details = []
        for d in rc.get("Details") or []:
            target = d.get("Target") or {}
            details.append({
                "attribute": target.get("Attribute"), "name": target.get("Name"), "path": target.get("Path"),
                "requires_recreation": target.get("RequiresRecreation"),
                "before": c.maybe_json(target.get("BeforeValue")), "after": c.maybe_json(target.get("AfterValue")),
                "change_type": target.get("AttributeChangeType"), "before_from": target.get("BeforeValueFrom"),
                "drift": target.get("Drift"), "evaluation": d.get("Evaluation"),
                "change_source": d.get("ChangeSource"), "causing_entity": d.get("CausingEntity"),
            })
        row = {
            "logical_id": logical, "action": rc.get("Action"), "resource_type": rc.get("ResourceType"),
            "physical_id": rc.get("PhysicalResourceId"), "replacement": rc.get("Replacement"),
            "scope": rc.get("Scope") or [], "policy_action": rc.get("PolicyAction"),
            "drift_status": rc.get("ResourceDriftStatus"), "details": details,
            "after_context": c.maybe_json(rc.get("AfterContext")) if rc.get("Action") == "Add" else None,
        }
        row["risk"], row["risk_reasons"] = assess(row)
        rows.append(row)
        if rc.get("NestedChangeSet"):
            rows.extend(normalize(rc["NestedChangeSet"], prefix=logical))
    return rows


def changed_props(row: dict, limit: int = 6) -> str:
    names: list[str] = []
    for d in row["details"]:
        label = c.property_label(d)
        if label and label not in names:
            names.append(label)
    return ", ".join(names[:limit]) + (" …" if len(names) > limit else "") if names else "—"


def summary_markdown(report: dict) -> str:
    if report["status"] == "NO_CHANGES":
        return ("## 📝 Change set — ✅ no changes\nThe template and parameters match what is deployed. "
                "The empty change set was deleted; nothing to approve.")
    if report["status"] == "FAILED":
        md = ["## 📝 Change set — ❌ failed", f"> {report.get('status_reason', '')}"]
        if report.get("validation_events"):
            md.append(c.md_table(["Check", "Mode", "Resource", "Path", "Reason"], [
                [e.get("validation") or e.get("hook") or e.get("type"), e.get("mode") or "", e.get("logical_id") or "",
                 e.get("path") or "", c.short(e.get("reason"), 220)] for e in report["validation_events"]]))
        return "\n\n".join(md)
    counts = report["counts"]
    risk_icon = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟢", "NONE": "🟢"}[report["risk"]]
    md = [f"## 📝 Change set `{report['change_set_name']}` — {risk_icon} risk {report['risk']}",
          f"**{counts.get('Add', 0)}** add · **{counts.get('Modify', 0)}** modify · **{counts.get('Remove', 0)}** remove"
          f" · **{report['replacements']}** replacement(s) · type `{report['change_set_type']}`"
          + (" · **drift-aware (REVERT_DRIFT)**" if report.get("deployment_mode") == "REVERT_DRIFT" else ""),
          f"[Open in the CloudFormation console]({report['console_url']})"]
    md.append(c.md_table(["Action", "Resource", "Type", "Replace", "Risk", "Properties"], [
        [r["action"], f"`{r['logical_id']}`", r["resource_type"], r["replacement"] or "—", r["risk"], changed_props(r)]
        for r in report["changes"][:150]]))
    detail_rows = []
    for r in report["changes"]:
        for d in r["details"]:
            if d.get("before") is not None or d.get("after") is not None:
                detail_rows.append([f"`{r['logical_id']}`", c.property_label(d),
                                     c.short(d.get("before"), 90), c.short(d.get("after"), 90),
                                     d.get("requires_recreation") or ""])
    if detail_rows:
        md.append("<details><summary>Property-level before → after</summary>\n\n"
                  + c.md_table(["Resource", "Property", "Before", "After", "Recreate"], detail_rows[:200])
                  + "\n</details>")
    warnings = [e for e in report.get("validation_events") or [] if e.get("type") != "LOOKUP_ERROR"]
    if warnings:
        md.append("**Pre-deployment validation warnings**\n" + "\n".join(
            f"- ⚠️ {e.get('validation') or e.get('hook')}: {e.get('logical_id') or ''} {e.get('reason')}" for e in warnings))
    notes = report.get("context_notes") or []
    if notes:
        md.append("\n".join(f"- ℹ️ {n}" for n in notes))
    md.append(f"```bash\n# execute after approval\naws cloudformation execute-change-set --region {report['region']} "
              f"--change-set-name '{report['change_set_id']}'\n```")
    return "\n\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/changeset.json")
    ap.add_argument("--template-url-file", default="")
    args = ap.parse_args()

    stack_name = c.env("STACK_NAME")
    region = c.env("TARGET_REGION", "eu-west-1")
    run = c.run_context()
    # Named after the *current* run attempt, so "Re-run failed jobs" never collides with an earlier change set.
    cs_name = f"gha-{run['run_id']}-{run['run_attempt']}"
    report: dict = {"stack_name": stack_name, "region": region, "change_set_name": cs_name,
                    "run": run, "template_file": c.env("TEMPLATE_FILE"), "created_at": c.utcnow(),
                    "status": "FAILED", "changes": [], "counts": {}, "replacements": 0, "risk": "NONE"}
    try:
        create_and_assess(report, args.template_url_file)
    except Exception as exc:  # API rejected the template/parameters, permissions, timeouts...
        report.update(status="FAILED", status_reason=c.error_message(exc))
        c.error(c.error_message(exc), title="Change set failed")

    c.write_json(args.out, report)
    for key in ("change_set_name", "change_set_type", "status", "risk"):
        c.set_output(key, report.get(key, ""))
    c.set_output("change_set_arn", report.get("change_set_id", ""))
    c.set_output("stack_id", report.get("stack_id", ""))
    c.set_output("total_changes", len(report.get("changes") or []))
    c.set_output("console_url", report.get("console_url", ""))
    c.add_summary(summary_markdown(report))
    return 1 if report["status"] == "FAILED" else 0


def create_and_assess(report: dict, template_url_file: str) -> None:
    stack_name, region, cs_name = report["stack_name"], report["region"], report["change_set_name"]
    template_file = report["template_file"]
    params_file = c.env("PARAMS_FILE")
    revert = c.env_bool("REVERT_DRIFT")
    poll = c.env_float("CFN_POLL_SECONDS", 5)
    run = report["run"]
    cfn = c.aws_client("cloudformation", region)

    template_text = Path(template_file).read_text(encoding="utf-8")
    template_info = t.describe_template(t.load_template_text(template_text))
    params = t.load_params(params_file)

    url = Path(template_url_file).read_text(encoding="utf-8").strip() if template_url_file else ""
    source = {"TemplateURL": url} if url else {"TemplateBody": template_text}
    report["template_source"] = "s3-presigned-url" if url else "inline"

    stack = ops.describe_stack(cfn, stack_name)
    cs_type = "CREATE" if ops.is_new_stack(stack) else "UPDATE"
    report["change_set_type"] = cs_type
    notes: list[str] = []

    if cs_type == "UPDATE":
        # Same rule as `aws cloudformation deploy` (the old pipeline): a template parameter that is not in
        # params.json keeps its currently deployed value instead of silently falling back to the Default.
        deployed = {p.get("ParameterKey") for p in (stack or {}).get("Parameters") or []}
        supplied = {p["ParameterKey"] for p in params}
        kept = [k for k in template_info["parameters"] if k not in supplied and k in deployed]
        params += [{"ParameterKey": k, "UsePreviousValue": True} for k in kept]
        if kept:
            notes.append("Not in params.json, so the deployed value is kept: " + ", ".join(kept) + ".")
    report["parameters"] = t.display_params(params, template_info)

    with c.group("Validate template with CloudFormation"):
        validation = cfn.validate_template(**source)
        needed = validation.get("Capabilities") or []
        print(f"Capabilities required: {needed or 'none'}; transforms: {validation.get('DeclaredTransforms') or 'none'}")
        if validation.get("CapabilitiesReason"):
            notes.append(f"Capabilities: {validation['CapabilitiesReason']}")

    if stack and cs_type == "UPDATE":
        try:
            if cfn.get_stack_policy(StackName=stack_name).get("StackPolicyBody"):
                notes.append("A stack policy is attached — updates to protected resources will fail at execution.")
        except Exception:  # informational only
            pass
        if stack.get("EnableTerminationProtection"):
            notes.append("Termination protection is ON.")
        try:
            pending = [s for s in c.paginate(cfn.list_change_sets, "Summaries", StackName=stack_name)
                       if s.get("ExecutionStatus") == "AVAILABLE"]
            if pending:
                notes.append(f"{c.plural(len(pending), 'other change set')} waiting on this stack: "
                             + ", ".join(s.get("ChangeSetName", "?") for s in pending[:5])
                             + ". Executing one makes the others OBSOLETE.")
        except Exception:
            pass
    report["context_notes"] = notes

    kwargs = {
        "StackName": stack_name, "ChangeSetName": cs_name, "ChangeSetType": cs_type,
        "Parameters": params, "Capabilities": CAPABILITIES, "ClientToken": cs_name,
        "Description": (f"GitHub Actions {run['repository']} run {run['run_id']}.{run['run_attempt']} by "
                        f"{run['actor']} @ {run['short_sha'] or 'local'}")[:1024],
        **source,
    }
    if template_info["nested_stacks"]:
        kwargs["IncludeNestedStacks"] = True
    if revert and cs_type == "UPDATE":
        kwargs["DeploymentMode"] = "REVERT_DRIFT"
        report["deployment_mode"] = "REVERT_DRIFT"

    with c.group(f"Create {cs_type} change set {cs_name}"):
        created = cfn.create_change_set(**kwargs)
        cs_id, stack_id = created["Id"], created.get("StackId", (stack or {}).get("StackId", ""))
        report.update(change_set_id=cs_id, stack_id=stack_id, account_id=c.account_from_arn(cs_id),
                      console_url=c.change_set_console_url(region, stack_id, cs_id))
        cs = ops.wait_for_change_set(cfn, cs_id, c.env_int("CHANGE_SET_TIMEOUT_SECONDS", 1800), poll)
        print(f"Status: {cs.get('Status')} {cs.get('StatusReason') or ''}")

    reason = cs.get("StatusReason") or ""
    if cs.get("Status") == "FAILED" and any(h in reason for h in NO_CHANGE_HINTS):
        report.update(status="NO_CHANGES", status_reason=reason, risk="NONE", changes=[], counts={},
                      replacements=0)
        try:
            cfn.delete_change_set(ChangeSetName=cs_id)
        except Exception as exc:
            c.warning(f"Could not delete the empty change set: {c.error_message(exc)}")
        c.notice("Stack already matches the template and parameters — nothing to deploy.", title="No changes")
    elif cs.get("Status") != "CREATE_COMPLETE":
        report.update(status="FAILED", status_reason=reason,
                      validation_events=ops.operation_events(cfn, change_set=cs_id, stack=stack_name, failed_only=True))
        c.error(reason or "Change set creation failed", title="Change set failed")
        for e in report["validation_events"][:20]:
            c.error(f"{e.get('logical_id') or ''} {e.get('path') or ''}: {e.get('reason')}",
                    title=str(e.get("validation") or e.get("hook") or e.get("type")))
        if cs_type == "CREATE":
            report["context_notes"].append(
                "A new stack in REVIEW_IN_PROGRESS was left behind with no resources; delete it if you do not re-run.")
    else:
        full = ops.describe_change_set_full(cfn, cs_id)
        rows = normalize(full)
        counts = Counter(r["action"] for r in rows)
        report.update(
            status="CREATE_COMPLETE", status_reason=reason, changes=rows, counts=dict(counts),
            replacements=sum(1 for r in rows if r["replacement"] in {"True", "Conditional"}),
            risk=c.max_risk(*(r["risk"] for r in rows)) if rows else "LOW",
            stack_drift_status=full.get("StackDriftStatus"), deployment_mode=full.get("DeploymentMode"),
            validation_events=[e for e in ops.operation_events(cfn, change_set=cs_id, stack=stack_name)
                               if e.get("type") != "LOOKUP_ERROR"],
            execution_status=full.get("ExecutionStatus"),
        )
        high = [r for r in rows if r["risk"] == "HIGH"]
        msg = f"{len(rows)} change(s), risk {report['risk']}" + (
            f" — HIGH: {', '.join(r['logical_id'] for r in high[:5])}" if high else "")
        (c.warning if report["risk"] == "HIGH" else c.notice)(msg, title=f"Change set {cs_name} ready for approval")


if __name__ == "__main__":
    sys.exit(main())
