Deployment pipeline review · October 2026

# CloudFormation deployment flows

The current workflow takes a template from the repository straight into a live AWS account in one step. Nothing previews the change, checks for edits made in the console, or asks anyone to approve it. The proposed pipeline puts automated checks in front of every change and runs it only after a person approves.

Current: `latest_cft_deployment.yml` · Proposed: `cfn-deploy-pipeline.yml` · Both are started manually from GitHub Actions

## At a glance

| Safeguard | Current flow | Proposed flow |
| --- | --- | --- |
| Template lint and security scan | None | cfn-lint and Checkov on every run |
| Drift check before deploying | None | Stack and nested stacks; drift stops the run |
| New stacks and new resources | Created as soon as the run starts | Shown as Add in the change set; name clashes and invalid properties stop the run first |
| Preview of what will change | None | Change set with a risk rating for every change |
| Approval before changes run | None | Required; a person executes the change set |
| Notifications and record | None | Emails for drift, approval and result; reports kept 30 to 90 days |
| Failure diagnosis | CREATE_FAILED events only | Every failed event, nested stacks included, with the root cause |

## Proposed flow

Each box is a stage of the GitHub Actions run. Steps 2 and 3 run side by side, and both must pass before a change set is made. Every blue exit ends the run before anything in AWS changes.

- Step
- Automated check
- Decision
- Safe stop: nothing changes in AWS
- Outcome
- Email sent

## Current flow

Traced step by step from `latest_cft_deployment.yml`. The numbered markers point to the risks listed below the diagram.

- Step
- Risk, numbered as in the table below
- Change goes live without review
- Safeguards that are missing

## What can go wrong in the current flow

| # | In the current flow | What can happen | Proposed flow |
| --- | --- | --- | --- |
| 1 | Run inputs are pasted straight into shell commands. | A mistyped or crafted value runs as a command on the runner, with the deploy role's AWS access. | Inputs are passed as data, never as code. The account is picked from a fixed list. |
| 2 | The template is picked with `find … \| head -n 1`. | If a folder holds two templates, either one can be deployed, with no warning. | Exactly one template per folder, or the run stops before AWS is touched. |
| 3 | No lint, security scan or drift check. | Template errors and insecure settings are found only in the live account. Changes made in the console are silently overwritten or break the deployment. | cfn-lint, Checkov and drift detection, nested stacks included, on every run. Drift stops the run and the email includes the suggested fix. |
| 4 | Changes go live as soon as the run starts. | Replacements and deletions, including resources that hold data, happen with no preview and no approval. | A change set is created and every change is rated. A person executes it only after approval. |
| 5 | Parameters are passed unquoted (`$param_args`). | Values with spaces or commas are split or misread, so the wrong value is deployed or the run fails. | Parameters go to the AWS API exactly as written in params.json. |
| 6 | Pre-signed S3 URLs are printed in the job log. | Anyone who can read the logs can upload or download the template until the URLs expire. | The URLs are masked and never printed. |
| 7 | Failures show only `CREATE_FAILED` events. | Update failures go unexplained. For small templates the diagnostics never run, and a large template with no changes fails the run. | Every failed event, nested stacks included, with the root cause. A run with no changes ends cleanly. |
| 8 | A failed first deployment leaves the stack in `ROLLBACK_COMPLETE`. | Every later run fails with a CloudFormation error and no guidance. | The run stops at the drift check and names the exact command that fixes it. |
| 9 | Nothing stops two runs on the same stack. | Two people can deploy to the same stack at the same time. | One run per stack; a second run waits for the first. |
| 10 | No email and no record of who approved. | Nobody is told what changed, who ran it or whether it worked. | Emails at drift, approval and result. Reports are kept for 30 to 90 days. |

Both diagrams are drawn from the workflow files in the repository. The proposed pipeline runs only when someone starts it, and nothing is deployed until a person executes the change set: in the AWS console, or by approving the GitHub environment gate.