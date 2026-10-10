#!/usr/bin/env python3
"""Stage 2a · Turn cfn-lint JSON into annotations, a job summary and lint-report.json.

Errors block the pipeline; warnings and informational findings are reported only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402

LEVEL_ICON = {"Error": "❌", "Warning": "⚠️", "Informational": "ℹ️"}


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

    try:
        matches = parse(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        c.error(f"cfn-lint did not produce readable JSON output: {exc}", title="Lint")
        c.write_json(args.out, {"tool": "cfn-lint", "error": str(exc), "errors": 1, "warnings": 0, "info": 0,
                                "findings": []})
        return 1

    findings = []
    for m in matches:
        rule = m.get("Rule") or {}
        start = ((m.get("Location") or {}).get("Start") or {})
        findings.append({
            "level": m.get("Level", "Error"),
            "rule": rule.get("Id", "?"),
            "message": m.get("Message", ""),
            "line": start.get("LineNumber"),
            "path": "/".join(str(p) for p in ((m.get("Location") or {}).get("Path") or [])),
            "doc": rule.get("Source", ""),
        })
    counts = {lvl: sum(1 for f in findings if f["level"] == lvl) for lvl in ("Error", "Warning", "Informational")}

    for f in findings[:50]:
        level = {"Error": "error", "Warning": "warning"}.get(f["level"], "notice")
        c.annotate(level, f"{f['rule']}: {f['message']}", title="cfn-lint", file=args.template, line=f["line"])

    report = {"tool": "cfn-lint", "template": args.template, "errors": counts["Error"],
              "warnings": counts["Warning"], "info": counts["Informational"], "findings": findings}
    c.write_json(args.out, report)
    c.set_output("errors", counts["Error"])
    c.set_output("warnings", counts["Warning"])

    status = "❌ failed" if counts["Error"] else ("⚠️ passed with warnings" if counts["Warning"] else "✅ clean")
    md = [f"## 🧹 cfn-lint — {status}",
          f"**{counts['Error']}** error(s) · **{counts['Warning']}** warning(s) · **{counts['Informational']}** info"]
    if findings:
        md.append(c.md_table(["", "Rule", "Line", "Message"],
                             [[LEVEL_ICON.get(f["level"], ""), f["rule"], f["line"] or "", c.short(f["message"], 200)]
                              for f in findings[:100]]))
    c.add_summary("\n\n".join(md))
    return 1 if counts["Error"] else 0


if __name__ == "__main__":
    sys.exit(main())
