#!/usr/bin/env python3
"""Stage 2b · Summarise Checkov's CloudFormation scan.

Report-only by default; set repository variable CFN_SECURITY_SCAN_BLOCKING=true to make
failed checks stop the pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402


def parse(raw: str) -> list[dict]:
    """Parse tool JSON output, skipping any log lines printed before it."""
    raw = raw.strip()
    if not raw:
        return []
    lines = raw.splitlines()
    last_error: Exception | None = None
    for index, line in enumerate(lines):
        if not line.lstrip().startswith(("[", "{")):
            continue
        try:
            data = json.loads("\n".join(lines[index:]))
        except ValueError as exc:
            last_error = exc
            continue
        return data if isinstance(data, list) else [data]
    raise ValueError(f"no JSON document found ({last_error})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    blocking = c.env_bool("SECURITY_BLOCKING")

    try:
        blocks = parse(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        c.warning(f"Checkov output could not be read ({exc}); security scan skipped.", title="Security scan")
        c.write_json(args.out, {"tool": "checkov", "error": str(exc), "failed": 0, "passed": 0, "findings": []})
        c.add_summary("## 🛡️ Security scan — ⚪ skipped (Checkov output unreadable)")
        return 1 if blocking else 0

    failed, passed, skipped, parse_errors = [], 0, 0, 0
    for block in blocks:
        results = block.get("results") or {}
        summary = block.get("summary") or block
        passed += int(summary.get("passed", len(results.get("passed_checks") or [])) or 0)
        skipped += int(summary.get("skipped", 0) or 0)
        parse_errors += int(summary.get("parsing_errors", 0) or 0)
        for chk in results.get("failed_checks") or []:
            lines = chk.get("file_line_range") or [None]
            failed.append({
                "id": chk.get("check_id"),
                "name": chk.get("check_name"),
                "resource": chk.get("resource"),
                "severity": chk.get("severity") or "",
                "line": lines[0],
                "guideline": chk.get("guideline") or "",
            })

    for f in failed[:30]:
        c.warning(f"{f['id']} {f['name']} ({f['resource']})", title="Checkov", file=args.template, line=f["line"])

    report = {"tool": "checkov", "template": args.template, "failed": len(failed), "passed": passed,
              "skipped": skipped, "parsing_errors": parse_errors, "blocking": blocking, "findings": failed}
    c.write_json(args.out, report)
    c.set_output("failed", len(failed))

    if failed:
        status = "❌ failed (blocking)" if blocking else "⚠️ findings (report-only)"
    else:
        status = "✅ clean"
    md = [f"## 🛡️ Security scan (Checkov) — {status}",
          f"**{len(failed)}** failed · **{passed}** passed · **{skipped}** skipped"]
    if failed:
        md.append(c.md_table(["Check", "Resource", "Severity", "Line", "Description"],
                             [[f["id"], f["resource"], f["severity"] or "—", f["line"] or "", c.short(f["name"], 120)]
                              for f in failed[:100]]))
    if parse_errors:
        md.append(f"⚠️ Checkov could not parse {parse_errors} file(s).")
    c.add_summary("\n\n".join(md))
    return 1 if (blocking and failed) else 0


if __name__ == "__main__":
    sys.exit(main())
