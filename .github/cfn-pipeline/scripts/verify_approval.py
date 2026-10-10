#!/usr/bin/env python3
"""Stage 7a · Safety check before executing: was this run really approved by a reviewer?

If the approval environment exists without "Required reviewers", GitHub would let the job
run straight away. This script reads the run's approval history and refuses to continue
unless a reviewer approved the configured environment.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as c  # noqa: E402


def fetch_approvals(api: str, repo: str, run_id: str, token: str) -> list[dict]:
    request = urllib.request.Request(
        f"{api}/repos/{repo}/actions/runs/{run_id}/approvals",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed GitHub API URL
        return json.load(response)


def main() -> int:
    env_name = c.env("APPROVAL_ENVIRONMENT", "cfn-change-approval")
    try:
        approvals = fetch_approvals(c.env("GITHUB_API_URL", "https://api.github.com"), c.env("GITHUB_REPOSITORY"),
                                    c.env("GITHUB_RUN_ID"), c.env("GH_TOKEN"))
    except (urllib.error.URLError, ValueError) as exc:
        c.error(f"Could not read the approval history for this run: {exc}", title="Approval check")
        return 1

    approved = [a for a in approvals if a.get("state") == "approved"
                and any((e or {}).get("name") == env_name for e in a.get("environments") or [])]
    if not approved:
        c.error(f"No reviewer approval is recorded for environment '{env_name}'. In Settings → Environments → "
                f"{env_name}, add Required reviewers (and tick 'Prevent self-review'), then re-run this job.",
                title="Execution blocked — no approval")
        c.add_summary(f"## 🚀 Execute — ⛔ blocked\nEnvironment `{env_name}` has no recorded approval for this run.")
        return 1

    approvers = sorted({(a.get("user") or {}).get("login", "?") for a in approved})
    comment = next((a.get("comment") for a in approved if a.get("comment")), "")
    starter = c.env("GITHUB_TRIGGERING_ACTOR") or c.env("GITHUB_ACTOR")
    if starter in approvers and len(approvers) == 1:
        message = (f"Run started and approved by the same person ({starter}). "
                   f"Enable 'Prevent self-review' on environment '{env_name}'.")
        if c.env_bool("BLOCK_SELF_APPROVAL"):
            c.error(message, title="Self-approval blocked")
            return 1
        c.warning(message, title="Self-approval")

    c.set_output("approver", ", ".join(approvers))
    c.set_output("approval_comment", comment)
    c.notice(f"Approved by {', '.join(approvers)}" + (f": “{comment}”" if comment else ""), title="Approval verified")
    c.add_summary(f"**Approval:** ✅ {', '.join(approvers)} on `{env_name}`" + (f" — “{comment}”" if comment else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
