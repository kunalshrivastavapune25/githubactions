#!/usr/bin/env python3
"""Build and send the pipeline emails.

  notify.py drift     -> drift alert (deployment halted) with property diffs, live YAML and Amazon Q code
  notify.py approval  -> "approval needed" observation for the change set (or "no changes")
  notify.py result    -> outcome of an executed change set

Channels (CFN_NOTIFY_CHANNEL): ses (HTML + attachments, default) | sns (plain text) | none.
The rendered email is always saved under reports/email/ and uploaded as an artifact, so it
can be previewed even when sending is switched off.
"""
from __future__ import annotations

import argparse
import csv
import email.policy
import io
import os
import sys
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402
import email_render as er  # noqa: E402
from email_render import Raw, badge, code_inline, join, link  # noqa: E402

REPORT_FILES = {"drift": "drift-report.json", "ai": "ai-suggestions.json", "changeset": "changeset.json",
                "lint": "lint-report.json", "security": "security-report.json", "execution": "execution-result.json"}


# ------------------------------------------------------------------ shared bits

def context() -> dict:
    return {
        "stack": c.env("STACK_NAME"), "region": c.env("TARGET_REGION", "eu-west-1"), "alias": c.env("ROLE_ALIAS", "?"),
        "tier": (c.env("TIER") or "?").upper(), "execution_mode": c.env("EXECUTION_MODE", "manual-console"),
        "drift_policy": c.env("DRIFT_POLICY", "block"),
        "approval_env": c.env("APPROVAL_ENVIRONMENT", "cfn-change-approval"),
        "template_file": c.env("TEMPLATE_FILE"), "params_file": c.env("PARAMS_FILE"),
        "prefix": c.env("MAIL_SUBJECT_PREFIX"), "run": c.run_context(),
    }


def subject(ctx: dict, tag: str, text: str) -> str:
    prefix = f"{ctx['prefix']} " if ctx["prefix"] else ""
    return f"{prefix}[{tag}][{ctx['tier']}] {ctx['stack']} — {text}"


def subtitle(ctx: dict, extra: str = "") -> str:
    return f"{ctx['alias']} ({ctx['tier']}) · {ctx['region']}" + (f" · {extra}" if extra else "")


def footer(ctx: dict) -> Raw:
    run = ctx["run"]
    return join(f"Sent by the “{run['workflow']}” workflow ·", link(f"run #{run['run_id']}", run["run_url"]),
                f"· started by @{run['actor']} · commit {run['short_sha'] or 'n/a'}. Reply-all to discuss with the team.")


def facts(ctx: dict, account_id: str = "", extra: list | None = None) -> list:
    run = ctx["run"]
    rows = [("Stack", code_inline(ctx["stack"])),
            ("Account", f"{ctx['alias']} · {account_id}" if account_id else ctx["alias"]),
            ("Region", ctx["region"])]
    rows += extra or []
    rows += [("Started by", f"@{run['actor']}"),
             ("Commit", link(run["short_sha"], run["commit_url"]) if run["commit_url"] else "n/a"),
             ("Pipeline run", link(f"#{run['run_id']} (attempt {run['run_attempt']})", run["run_url"]))]
    return rows


plural = c.plural


def resource_cell(logical_id: str, resource_type: str, note: str = "") -> Raw:
    """Logical ID with its type underneath — keeps tables narrow enough for mail clients."""
    sub = er.esc(resource_type) + (f" · {er.esc(note)}" if note else "")
    return Raw(f'{code_inline(logical_id).html}<div style="font-size:12px;color:{er.MUTED};margin-top:3px;">{sub}</div>',
               f"{logical_id} ({resource_type}{', ' + note if note else ''})")


def status_badge(status: str) -> Raw:
    tone = "danger" if "FAILED" in status or "ROLLBACK" in status else ("success" if status.endswith("COMPLETE") else "neutral")
    return badge(status, tone)


# ------------------------------------------------------------------ drift alert

def drifted_resource_section(mail: er.Email, d: dict, s: dict | None, ctx: dict) -> None:
    mail.divider()
    mail.heading(f"{d['logical_id']} · {d['resource_type']}", level=3)
    where = [] if d.get("is_root_stack", True) else [f"· nested stack {d.get('stack')}"]
    mail.paragraph(join(badge(d["status"]), "Physical ID", code_inline(d.get("physical_id") or "—"), *where), muted=True)
    if d.get("differences"):
        mail.table(["Property", "In template (expected)", "Live in AWS (actual)", "Change"],
                   [[code_inline(x["path"]), c.short(x["expected"], 140), c.short(x["actual"], 140), x.get("type") or ""]
                    for x in d["differences"][:40]])
    elif d["status"] == "DELETED":
        mail.callout("This resource was deleted outside CloudFormation but is still part of the stack. "
                     "Recreate it by removing and re-adding it in the template, or restore it and import it back.",
                     "danger")

    if s and s.get("ok"):
        tone = {"REVERT_TO_TEMPLATE": "danger", "ADOPT_LIVE": "info"}.get(s["recommendation"], "warn")
        mail.callout(join(badge(s["recommendation"].replace("_", " ")), badge(f"RISK {s['risk']}", er.BADGE_TONES.get(s["risk"])),
                          Raw("<br>", "\n"), s.get("summary") or ""), tone, title=f"Suggested fix · {s.get('provider_label')}")
        if s.get("impact"):
            mail.paragraph(join(Raw("<b>Impact:</b>", "Impact:"), s["impact"]))
        if s.get("rationale"):
            mail.paragraph(join(Raw("<b>Why:</b>", "Why:"), s["rationale"]))
        if s.get("updated_resource_yaml"):
            check = "" if s.get("yaml_valid") else "  (review: did not parse cleanly)"
            mail.code(s["updated_resource_yaml"], title=f"Suggested code for {ctx['template_file'] or 'your template'}{check}")
        if s.get("parameter_changes"):
            mail.table(["Parameter (params.json)", "New value", "Why"],
                       [[code_inline(p["ParameterKey"]), code_inline(p["NewValue"]), p["Reason"]] for p in s["parameter_changes"]])
        mail.bullets(s.get("review_notes") or [])
    elif s:
        mail.paragraph(f"No suggested fix for this resource ({c.short(s.get('error'), 200)}).", muted=True)

    if d.get("live_yaml"):
        mail.code(d["live_yaml"], title="Live YAML of this resource (drift-tracked properties, resolved values)")
    if d.get("template_snippet"):
        mail.code(d["template_snippet"], title=f"Current code in {ctx['template_file']}", max_lines=80)
    if d.get("physical_id"):
        mail.code(f"aws cloudtrail lookup-events --region {ctx['region']} --max-results 20 \\\n"
                  f"  --lookup-attributes 'AttributeKey=ResourceName,AttributeValue={d['physical_id']}'",
                  title="Who changed it? (CloudTrail, last 90 days)")


def build_drift(ctx: dict, reports: dict, reports_dir: Path) -> er.Email:
    drift = reports.get("drift") or {}
    ai = reports.get("ai") or {}
    reason = drift.get("block_reason") or "ERROR"
    drifted = drift.get("drifted_resources") or []
    # keyed by (stack, logical id): nested stacks can reuse logical IDs from the root stack
    suggestions = {(s.get("stack"), s.get("logical_id")): s for s in ai.get("suggestions") or []}
    n = len(drifted)

    if reason == "DRIFT":
        mail = er.Email(subject(ctx, "DRIFT", f"deployment halted, {plural(n, 'drifted resource')}"),
                        "Drift detected — deployment halted", subtitle(ctx), "danger",
                        preheader=f"{plural(n, 'resource')} in {ctx['stack']} changed outside CloudFormation.")
    elif reason == "INCONCLUSIVE":
        mail = er.Email(subject(ctx, "DRIFT CHECK INCONCLUSIVE", "deployment halted"),
                        "Drift check inconclusive — deployment halted", subtitle(ctx), "warn")
    elif reason == "STACK_STATE":
        status = drift.get("stack_status", "?")
        mail = er.Email(subject(ctx, "BLOCKED", f"stack is {status}"), f"Deployment blocked — stack is {status}",
                        subtitle(ctx), "danger")
    else:
        mail = er.Email(subject(ctx, "PIPELINE ERROR", "drift check failed"), "Drift check failed — deployment halted",
                        subtitle(ctx), "danger")

    message = drift.get("message") or "The drift stage did not finish."
    mail.callout(f"{message} No change set was created, so this run did not change anything in AWS.",
                 "warn" if reason == "INCONCLUSIVE" else "danger")
    mail.facts(facts(ctx, drift.get("account_id", ""), [
        ("Stack status", drift.get("stack_status") or "—"),
        ("Drift checked", c.iso(drift.get("checked_at"))),
        ("Drift policy", ctx["drift_policy"]),
    ]))
    mail.buttons([("View drift in AWS console", drift.get("drift_console_url", ""), "primary"),
                  ("Open pipeline run", ctx["run"]["run_url"], "secondary")])

    if reason == "DRIFT":
        mail.heading("Drifted resources")
        rows = []
        for d in drifted:
            s = suggestions.get((d.get("stack"), d["logical_id"]))
            note = "" if d.get("is_root_stack", True) else f"nested: {d['stack']}"
            rows.append([resource_cell(d["logical_id"], d["resource_type"], note), badge(d["status"]),
                         ", ".join(x["path"] for x in d.get("differences", [])[:5]) or "—",
                         badge(s["recommendation"].replace("_", " "), er.BADGE_TONES.get(s["recommendation"])) if s and s.get("ok") else "—"])
        mail.table(["Resource", "Drift", "Changed properties", "Suggested fix"], rows)
        for d in drifted[:15]:
            drifted_resource_section(mail, d, suggestions.get((d.get("stack"), d["logical_id"])), ctx)
        if n > 15:
            mail.paragraph(f"…and {n - 15} more — see the attached drift-report.json.", muted=True)
        mail.divider()
        mail.heading("How to resolve")
        tf = ctx["template_file"] or "the template"
        mail.bullets([
            join(Raw("<b>Keep the live change</b>", "Keep the live change"),
                 f"— copy the suggested code (and any params.json changes) into {tf}, raise a PR, merge it and re-run "
                 "the pipeline. Drift detection will then report IN_SYNC."),
            join(Raw("<b>Undo the live change</b>", "Undo the live change"),
                 "— re-run the pipeline with driftPolicy = revert-drift. It creates a drift-aware change set that puts "
                 "the live resource back to the template values, then follows the normal approval flow."),
            join(Raw("<b>Not sure?</b>", "Not sure?"),
                 "— use the CloudTrail command under each resource to see who changed it, and agree with the owner first."),
        ])
        live = "\n".join(f"# {d['logical_id']} ({d['resource_type']}) — {d['status']} in {d['stack']}\n{d['live_yaml']}"
                         for d in drifted if d.get("live_yaml"))
        if live:
            mail.attach("drifted-resources-live.yaml", live, "text/yaml")
        code = "\n".join(f"# {s['logical_id']} — {s['recommendation']} (risk {s['risk']}) via {s.get('provider_label')}\n"
                         f"{s['updated_resource_yaml']}\n" for s in suggestions.values()
                         if s.get("ok") and s.get("updated_resource_yaml"))
        if code:
            mail.attach("suggested-code.yaml", code, "text/yaml")
        md = reports_dir / "suggested-fixes.md"
        if md.is_file():
            mail.attach("suggested-fixes.md", md.read_text(encoding="utf-8"), "text/markdown")
    elif reason == "INCONCLUSIVE":
        mail.heading("What could not be checked")
        mail.table(["Stack", "Detection status", "Reason"],
                   [[s.get("label"), s.get("status"), c.short(s.get("reason"), 200)] for s in drift.get("inconclusive") or []])
        mail.bullets(["Usually the deploy role lacks read permission for one resource type, or a type does not support "
                      "drift detection. Fix the permission and re-run.",
                      "If you accept the risk, re-run with driftPolicy = revert-drift: the drift-aware change set shows "
                      "live vs template values for every resource before you approve."])
    elif reason == "ERROR":
        if drift.get("error"):
            mail.code(drift["error"], title="Error")
        mail.bullets(["Open the pipeline run for the full log of the 🔍 Drift detection box.",
                      "Common causes: the deploy role does not trust this repository's OIDC subject, or it lacks "
                      "cloudformation:DetectStackDrift / DescribeStackDriftDetectionStatus / DescribeStackResourceDrifts "
                      "or read access to the stack's resource types."])
    if drift:
        mail.attach("drift-report.json", (reports_dir / REPORT_FILES["drift"]).read_bytes()
                    if (reports_dir / REPORT_FILES["drift"]).is_file() else b"{}", "application/json")
    return mail


# ------------------------------------------------------------------ approval request

def drift_fact(drift: dict) -> str:
    status = drift.get("drift_status")
    if status == "NEW_STACK":
        return "Skipped — new stack"
    if status == "IN_SYNC":
        return f"✅ In sync (checked {c.iso(drift.get('checked_at'))})"
    if status in {"DRIFTED", "UNKNOWN"} and drift.get("revert_drift"):
        return f"♻️ {plural(len(drift.get('drifted_resources') or []), 'drifted resource')} — this change set reverts them"
    return status or "—"


def changes_csv(changes: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Action", "LogicalId", "Type", "PhysicalId", "Replacement", "Risk", "RiskReasons", "Properties"])
    for r in changes:
        props = sorted({c.property_label(d) for d in r["details"]})
        writer.writerow([r["action"], r["logical_id"], r["resource_type"], r.get("physical_id") or "",
                         r.get("replacement") or "", r["risk"], "; ".join(r["risk_reasons"]), ", ".join(props)])
    return buf.getvalue()


def build_no_change(ctx: dict, cs: dict, drift: dict) -> er.Email:
    mail = er.Email(subject(ctx, "NO CHANGES", "nothing to deploy"), f"No changes for {ctx['stack']}",
                    subtitle(ctx), "neutral", preheader="The template already matches what is deployed.")
    mail.callout("The template and params.json match what is already deployed, and the stack has no drift. "
                 "The empty change set was deleted — there is nothing to approve.", "success")
    mail.facts(facts(ctx, c.account_from_arn(cs.get("stack_id")), [("Drift check", drift_fact(drift))]))
    mail.buttons([("Open pipeline run", ctx["run"]["run_url"], "primary")])
    return mail


def build_approval(ctx: dict, reports: dict, reports_dir: Path) -> er.Email:
    cs = reports.get("changeset") or {}
    drift = reports.get("drift") or {}
    if cs.get("status") == "NO_CHANGES":
        return build_no_change(ctx, cs, drift)
    lint, sec = reports.get("lint") or {}, reports.get("security") or {}
    changes = cs.get("changes") or []
    counts = cs.get("counts") or {}
    risk = cs.get("risk", "LOW")
    tone = {"HIGH": "danger", "MEDIUM": "warn"}.get(risk, "info")
    n = len(changes)
    region, arn = ctx["region"], cs.get("change_set_id", "")
    mail = er.Email(subject(ctx, "APPROVAL NEEDED", f"{plural(n, 'change')}, risk {risk}"),
                    f"Approval needed: {plural(n, 'change')} to {ctx['stack']}",
                    subtitle(ctx, f"change set {cs.get('change_set_name')}"), tone,
                    preheader=f"Risk {risk}. Review the change set and execute it after approval.")

    verbs = [("Add", "add"), ("Modify", "modify"), ("Remove", "remove"), ("Import", "import"),
             ("Dynamic", "dynamically update"), ("SyncWithActual", "re-sync")]
    parts = [f"{verb} {counts[a]}" for a, verb in verbs if counts.get(a)]
    sentence = f"This change set will {', '.join(parts) or 'change'} resource(s)"
    if cs.get("replacements"):
        sentence += f", including {plural(cs['replacements'], 'replacement')}"
    sentence += f". Overall risk: {risk}."
    if cs.get("deployment_mode") == "REVERT_DRIFT":
        sentence += " It is drift-aware: executing it also puts drifted resources back to their template values."
    mail.callout(sentence, tone, title="Observation")

    lint_txt = (f"{plural(lint.get('errors', 0), 'error')} · {plural(lint.get('warnings', 0), 'warning')}"
                if lint and not lint.get("error") else "—")
    sec_txt = (f"{plural(sec.get('failed', 0), 'finding')} · {'blocking' if sec.get('blocking') else 'report-only'}"
               if sec and not sec.get("error") else "—")
    mail.facts(facts(ctx, cs.get("account_id", ""), [
        ("Change set", code_inline(cs.get("change_set_name"))),
        ("Type", f"{cs.get('change_set_type')}" + (" · drift-aware (REVERT_DRIFT)" if cs.get("deployment_mode") == "REVERT_DRIFT" else "")),
        ("Risk", badge(risk)), ("Drift check", drift_fact(drift)), ("cfn-lint", lint_txt), ("Security scan", sec_txt),
    ]))
    mail.buttons([("Review change set in AWS console", cs.get("console_url", ""), "primary"),
                  ("Open pipeline run", ctx["run"]["run_url"], "secondary")])

    mail.heading("What will change")
    rows = []
    for r in changes[:120]:
        props = sorted({c.property_label(d) for d in r["details"]} - {""})
        rows.append([badge(r["action"]), resource_cell(r["logical_id"], r["resource_type"]), r.get("replacement") or "—",
                     badge(r["risk"]), c.short(", ".join(props), 120) if props else "—"])
    mail.table(["Action", "Resource", "Replace", "Risk", "Properties changed"], rows,
               note=f"Showing {len(rows)} of {n}. Full list in changes.csv." if n > len(rows) else None)

    flagged = [r for r in changes if r["risk"] in {"HIGH", "MEDIUM"} and r["risk_reasons"]]
    if flagged:
        mail.heading(f"Why the risk is {risk}")
        mail.bullets([join(code_inline(r["logical_id"]), badge(r["risk"]), "— " + "; ".join(r["risk_reasons"]))
                      for r in flagged[:25]])

    detail_rows = []
    for r in changes:
        for d in r["details"]:
            if d.get("before") is None and d.get("after") is None:
                continue
            drift_note = " (drift revert)" if d.get("before_from") == "ACTUAL_STATE" else ""
            detail_rows.append([resource_cell(r["logical_id"], c.property_label(d)),
                                c.short(d.get("before"), 110), c.short(d.get("after"), 110),
                                (d.get("requires_recreation") or "") + drift_note])
    if detail_rows:
        mail.heading("Property changes (before → after)")
        mail.table(["Resource · property", "Before", "After", "Recreate"], detail_rows[:60],
                   note=f"Showing 60 of {len(detail_rows)}." if len(detail_rows) > 60 else None)

    checks = [e for e in cs.get("validation_events") or [] if e.get("reason")]
    if checks:
        mail.heading("CloudFormation pre-deployment checks")
        mail.bullets([f"{e.get('validation') or e.get('hook') or e.get('type')} ({e.get('mode') or 'info'}): "
                      f"{e.get('logical_id') or ''} {e.get('reason')}" for e in checks[:15]])
    notes = list(cs.get("context_notes") or [])
    if lint.get("warnings"):
        notes.append("cfn-lint warnings: " + "; ".join(f"{f['rule']} {c.short(f['message'], 80)}"
                                                        for f in lint.get("findings", []) if f["level"] == "Warning")[:400])
    if sec.get("failed"):
        notes.append("Security findings: " + "; ".join(f"{f['id']} on {f['resource']}" for f in sec.get("findings", [])[:6]))
    if notes:
        mail.heading("Also worth knowing")
        mail.bullets(notes)

    mail.heading("How to approve and deploy")
    wait = "stack-create-complete" if cs.get("change_set_type") == "CREATE" else "stack-update-complete"
    cli = (f"# deploy (only after approval)\naws cloudformation execute-change-set --region {region} \\\n"
           f"  --change-set-name '{arn}'\naws cloudformation wait {wait} --region {region} --stack-name {ctx['stack']}\n\n"
           f"# reject: delete the change set\naws cloudformation delete-change-set --region {region} \\\n"
           f"  --change-set-name '{arn}'")
    if cs.get("change_set_type") == "CREATE":
        cli += f"\naws cloudformation delete-stack --region {region} --stack-name {ctx['stack']}   # empty REVIEW_IN_PROGRESS stack"
    if ctx["execution_mode"] == "pipeline-after-approval":
        mail.bullets([
            join("Open the", link("pipeline run", ctx["run"]["run_url"]), "→", Raw("<b>Review deployments</b>", "Review deployments"),
                 "→ tick", code_inline(ctx["approval_env"]), "→", Raw("<b>Approve and deploy</b>.", "Approve and deploy.")),
            "The pipeline re-checks drift, executes the change set, streams the stack events and emails the result.",
            "To reject: choose Reject in GitHub, then delete the change set with the command below.",
            "Executing from the console instead also works — the pipeline notices and just reports the outcome.",
        ], ordered=True)
    else:
        mail.bullets([
            "Review the change set in the AWS console (button above). Look closely at every Remove and Replacement.",
            join("If approved: click", Raw("<b>Execute change set</b>", "Execute change set"),
                 "in the console (keep the default “roll back all resources” on failure), or run the CLI command below."),
            "If rejected: delete the change set so nobody executes it by mistake.",
            "If anything else updates this stack first, the change set becomes OBSOLETE — just re-run the pipeline.",
        ], ordered=True)
    mail.code(cli, title="CLI alternative")
    mail.attach("changes.csv", changes_csv(changes), "text/csv")
    if (reports_dir / REPORT_FILES["changeset"]).is_file():
        mail.attach("changeset.json", (reports_dir / REPORT_FILES["changeset"]).read_bytes(), "application/json")
    return mail


# ------------------------------------------------------------------ execution result

def build_result(ctx: dict, reports: dict, reports_dir: Path) -> er.Email:
    res = reports.get("execution") or {}
    ok = bool(res.get("success"))
    final = res.get("final_stack_status") or res.get("status") or "UNKNOWN"
    duration = c.duration(res.get("duration_seconds"))
    if ok:
        mail = er.Email(subject(ctx, "DEPLOYED", final), f"Deployed: {ctx['stack']} is {final}", subtitle(ctx), "success")
        mail.callout(f"Change set {res.get('change_set_name')} was executed and finished in {duration}.", "success")
    else:
        mail = er.Email(subject(ctx, "DEPLOY FAILED", final), f"Deployment failed: {final}", subtitle(ctx), "danger")
        rc = res.get("root_cause") or {}
        text = (f"Root cause: {rc.get('logical_id')} ({rc.get('resource_type')}) — {rc.get('reason')}" if rc
                else res.get("error") or "See the stack events for details.")
        mail.callout(text, "danger")
    mail.facts(facts(ctx, c.account_from_arn(res.get("stack_id")), [
        ("Change set", code_inline(res.get("change_set_name") or "—")), ("Approved by", res.get("approver") or "—"),
        ("Final status", badge(final, "success" if ok else "danger")), ("Duration", duration if res.get("started_at") else "—"),
        ("Executed by", "Before this job (AWS console / CLI or an earlier attempt)" if res.get("already_executed") else "Pipeline"),
    ]))
    mail.buttons([("Open stack events", res.get("events_console_url", ""), "primary"),
                  ("Open pipeline run", ctx["run"]["run_url"], "secondary")])
    if res.get("failures"):
        mail.heading("Failed resources")
        mail.table(["Resource", "Status", "Reason"], [
            [resource_cell(f["logical_id"], f.get("resource_type") or "", f"nested: {f['nested_stack']}" if f.get("nested_stack") else ""),
             status_badge(f["status"]), c.short(f["reason"], 300)] for f in res["failures"][:40]])
    if res.get("operation_failures"):
        mail.heading("CloudFormation diagnostics")
        mail.bullets([f"{e['logical_id']}: {e.get('reason')}" if e.get("logical_id") else str(e.get("reason"))
                      for e in res["operation_failures"][:15]])
    if res.get("outputs"):
        mail.heading("Stack outputs")
        mail.table(["Output", "Value"], [[code_inline(o["key"]), c.short(o["value"], 300)] for o in res["outputs"]])
    if (reports_dir / REPORT_FILES["execution"]).is_file():
        mail.attach("execution-result.json", (reports_dir / REPORT_FILES["execution"]).read_bytes(), "application/json")
    return mail


# ------------------------------------------------------------------ sending

def has_aws_credentials() -> bool:
    return any(os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE"))


def mime_message(mail: er.Email, html: str, text: str, sender: str, to: list[str], cc: list[str]) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = mail.subject
    name, addr = parseaddr(sender)
    msg["From"] = formataddr((name or "CloudFormation pipeline", addr))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for filename, data, mime in mail.attachments:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes(policy=email.policy.SMTP)


def send(mail: er.Email, html: str, text: str, channel: str) -> tuple[bool, str]:
    if channel == "none":
        return False, "sending is switched off (CFN_NOTIFY_CHANNEL=none)"
    if not has_aws_credentials():
        return False, "no AWS credentials — set secret CFN_PIPELINE_SERVICES_ROLE"
    region = c.env("SERVICES_REGION", "eu-west-1")
    if channel == "sns":
        topic = c.env("SNS_TOPIC_ARN")
        if not topic:
            return False, "CFN_SNS_TOPIC_ARN is not set"
        plain = mail.subject.replace("—", "-").replace("→", "->").replace("·", "-")
        subj = " ".join(plain.encode("ascii", "ignore").decode().split())[:99]
        resp = c.aws_client("sns", region).publish(TopicArn=topic, Subject=subj, Message=text[:250_000])
        return True, f"SNS message {resp.get('MessageId')} → {topic}"
    sender = c.env("MAIL_FROM")
    to, cc = c.split_addresses(c.env("MAIL_TO")), c.split_addresses(c.env("MAIL_CC"))
    if not sender or not to:
        return False, "CFN_MAIL_FROM / CFN_MAIL_TO are not set"
    raw = mime_message(mail, html, text, sender, to, cc)
    destination = {"ToAddresses": to}
    if cc:
        destination["CcAddresses"] = cc
    resp = c.aws_client("sesv2", region).send_email(FromEmailAddress=parseaddr(sender)[1], Destination=destination,
                                                     Content={"Raw": {"Data": raw}})
    return True, f"SES message {resp.get('MessageId')} → {', '.join(to + cc)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["drift", "approval", "result"])
    ap.add_argument("--reports-dir", default="reports")
    ap.add_argument("--out-dir", default="reports/email")
    args = ap.parse_args()

    reports_dir = Path(args.reports_dir)
    reports = {key: c.read_json(reports_dir / name) for key, name in REPORT_FILES.items()}
    ctx = context()
    builder = {"drift": build_drift, "approval": build_approval, "result": build_result}[args.kind]
    mail = builder(ctx, reports, reports_dir)
    html, text = mail.render_html(footer(ctx)), mail.render_text(footer(ctx))

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.kind}.html").write_text(html, encoding="utf-8")
    (out / f"{args.kind}.txt").write_text(text, encoding="utf-8")

    channel = c.env("NOTIFY_CHANNEL", "ses").lower()
    try:
        sent, detail = send(mail, html, text, channel)
    except Exception as exc:  # SES sandbox, unverified identity, missing permission...
        sent, detail = False, f"send failed: {c.error_message(exc)}"
    c.set_output("email_sent", sent)
    c.set_output("subject", mail.subject)
    if sent:
        c.notice(detail, title="Email sent")
    else:
        c.warning(f"Email not sent — {detail}. Preview is in the 'email-{args.kind}' artifact.", title="Email")
    c.add_summary(f"### 📧 {mail.subject}\n{'✅ ' + detail if sent else '⚠️ Not sent — ' + detail}\n\n"
                  f"Attachments: {', '.join(a[0] for a in mail.attachments) or 'none'} · "
                  f"preview: artifact `email-{args.kind}`")
    if not sent and channel != "none" and c.env_bool("NOTIFY_STRICT"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
