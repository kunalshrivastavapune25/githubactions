GitHub Actions → AWS CodePipeline · CloudFormation change sets

# CloudFormation Gatekeeper Flow

One run of `latest_cft_deployment.yml`, start to finish. GitHub checks the input, stops on drift and builds the change set. With Deploy = yes, an account that has the gatekeeper pipeline needs an approver before AWS executes anything.

- Step
- Decision
- Start or end
- Stops – nothing deployed
- Approval path
- Finished

Swipe sideways to see the whole diagram.

The top band is one GitHub Actions run; the bottom band is the gatekeeper pipeline in the stack's own AWS account. Every red exit ends the run before anything is deployed. On the amber path the run files an approval request and ends green, and AWS executes the change set only after an approver says yes.

## What keeps the gate closed

One-time setup in every account that has the gatekeeper

- Deny the GitHub role `cloudformation:ExecuteChangeSet` and `codepipeline:PutApprovalResult`, so a run can neither execute directly nor approve itself.
- Give `codepipeline:PutApprovalResult` only to the approvers' SSO permission set.
- Only a "pipeline not found" answer sends a run down the direct path. Any other lookup error stops the run.

## Good to know

How the pieces behave day to day

- One gatekeeper per account and region. Accounts without one deploy directly when Deploy = yes.
- Requests don't block each other: each one waits for its own decision, and the waiting isn't billed.
- With no decision in 7 days the request expires and the change set is left untouched.
- The GitHub run ends green at "waiting for approval". The deploy result arrives as an outcome email.