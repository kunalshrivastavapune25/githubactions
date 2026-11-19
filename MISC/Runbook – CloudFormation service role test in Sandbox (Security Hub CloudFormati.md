# Runbook – CloudFormation service role test in Sandbox (Security Hub CloudFormation.4)

Nov 19, 2026 · @Kunal Shrivastava

## Purpose and scope

This test proves, in Sandbox only, that the new CloudFormation service role clears Security Hub control CloudFormation.4. A throwaway stack starts as Failed and should end as Passed after one pipeline run.

- **Account:** Sandbox, 036943221440. Nothing changes in IMS Root or in any other account.
- **You need:** admin access to the Sandbox account in the AWS console, write access to the pipeline repo, and a view of Security Hub.
- **Safe to stop at any point:** the test branch is never merged, and the new role does nothing unless the pipeline passes it.

| File | Where it goes | Step |
| --- | --- | --- |
| `sandbox-iam-roles-template.yml` | The Sandbox roles stack (console update), and the repo | 1 |
| `latest_cft_deployment.yml` | `.github/workflows/` on branch `test/cfn-service-role` | 2 |
| `cfn-role-test-template.yml` | `cloudformation/sandbox/cfn-role-test/` on the same branch | 3 |
| `params.json` (holds `[]`) | The same folder | 3 |

All four files are in `sandbox-cfn-role-test.zip`, sent with this runbook.

## Section 1 – Quick steps for developers

Do the three steps in order, all in Region eu-west-1. Step 1 must be finished before the pipeline run in step 3.

### Step 1 – Add the service role to the Sandbox roles stack (AWS console)

1. Sign in to the Sandbox account (036943221440) in the AWS console, Region eu-west-1.
2. CloudFormation → Stacks → open the stack that holds `iamr-sandbox-sbx-deploy`. It is probably `ims-sbx-sandbox-ims-sandbox`; its Resources tab lists the role.
3. Stack actions → Create change set for current stack → Replace existing template → upload `sandbox-iam-roles-template.yml`.
4. Keep `SourceAccountId` = 390403857119. Leave Permissions → IAM role empty. Tick the IAM acknowledgement.
5. Open the change set. It must show exactly one change: **Add** `CfnServiceRole` (`AWS::IAM::Role`). Anything on `DeployRole` or `ReadRole` → stop and do not execute.
6. Execute the change set and wait for `UPDATE_COMPLETE`.
7. IAM → Roles → `iamr-sandbox-sbx-cfn`. Trust relationships shows `cloudformation.amazonaws.com`; Permissions shows `AdministratorAccess`.
8. Commit the same template to the repo, so Git matches AWS.

**Done when:** `iamr-sandbox-sbx-cfn` exists and trusts only `cloudformation.amazonaws.com`.

### Step 2 – Put the new pipeline on a test branch (GitHub)

1. Create branch `test/cfn-service-role` from `main`.
2. Replace `.github/workflows/latest_cft_deployment.yml` with the new file and commit.
3. Add `cfn-role-test-template.yml` and `params.json` (below) in `cloudformation/sandbox/cfn-role-test/` and commit.
4. Open a draft pull request to `main`, only to read the diff. The workflow file should show 83 lines added and 4 removed, all about the service role. Any other difference means the repo copy changed after 17 Nov 2026 → stop and send the file over.
5. Do not merge.

**Done when:** the branch holds the three files and the draft PR shows only the service-role changes.

### Step 3 – Run the test in Sandbox

The test stack is one SSM parameter: no cost, no data, nothing depends on it.

`cfn-role-test-template.yml`:

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Throwaway test for the CloudFormation service role (Security Hub CloudFormation.4) in Sandbox - delete after the test

Resources:
  TestParameter:
    Type: AWS::SSM::Parameter
    Properties:
      Name: /platform/cfn-service-role-test
      Type: String
      Value: "v1"
      Description: Test only - deleted with the cfn-role-test stack
```

`params.json`:

```json
[]
```

1. **Before:** in the Sandbox console, CloudFormation → Create stack → upload `cfn-role-test-template.yml` as it is (`v1`). Stack name `cfn-role-test`, IAM role empty. Wait for `CREATE_COMPLETE`.
2. Security Hub → Controls → CloudFormation.4, filtered on account 036943221440: `cfn-role-test` appears as **Failed**. It can take a while to show.
3. On the branch, change `Value: "v1"` to `Value: "v2"` and commit.
4. GitHub → Actions → *CloudFormation Stack using S3 as required* → Run workflow, with:

| Field | Value |
| --- | --- |
| Use workflow from | `test/cfn-service-role` |
| Path under cloudformation/ | `sandbox/cfn-role-test` |
| Stack name | `cfn-role-test` |
| Account | `Sandbox` |
| AWS Region | `eu-west-1` |
| Deploy | `yes` |
| Remove manual changes | `no` |

5. Check the run:
   - Prepare: *Service role: none yet → iamr-sandbox-sbx-cfn (attached when this change set is executed)*.
   - Change set: *CloudFormation makes these changes as iamr-sandbox-sbx-cfn*, with 0 to add, 1 to modify, 0 to remove.
   - Deploy: *Deployed – the stack finished as UPDATE\_COMPLETE*. A *Deploying from a branch* warning is expected.
6. CloudFormation → `cfn-role-test` → Stack info → IAM role = `arn:aws:iam::036943221440:role/iamr-sandbox-sbx-cfn`.
7. Security Hub → CloudFormation.4: `cfn-role-test` moves to **Passed**. The check re-runs on its own after the stack changes; allow some time.
8. **Clean up:** delete stack `cfn-role-test` in the console (this also proves deletes work with the new role). Remove the test folder from the branch. Keep `iamr-sandbox-sbx-cfn`.

**Done when:** the stack showed Passed in Security Hub and has been deleted.

## Section 2 – Reference guide for non-developers

The alert clears when CloudFormation builds a stack with its own role instead of a borrowed one. This test gives it that role in Sandbox and proves one stack turns from Failed to Passed.

### What the alert means

CloudFormation is the builder that creates AWS resources from our templates. Security Hub check CloudFormation.4 wants every stack to have its own builder role recorded on it, called a service role. Our stacks have none, so CloudFormation borrows the pipeline's sign-in role, and the check fails.

### How it fits together

&#91;embedded content: who builds the stack · today and after the change\]

Only the last box changes: GitHub still signs in through the same two hops, then hands CloudFormation its own role.

### What each step does

| Step | What it does | Why it matters |
| --- | --- | --- |
| 1 – Add the role | Creates `iamr-sandbox-sbx-cfn`, a role that only CloudFormation can use | Gives the builder its own role |
| 2 – Change the pipeline | The pipeline hands that role to CloudFormation on every deploy (`--role-arn`) | Records the role on each stack it deploys |
| 3 – Run the test | One throwaway stack goes Failed → deploy → Passed | Shows the fix works before any other account |

The sign-in roles (IMS Root and the Sandbox deploy and read roles) do not change.

### Evidence to capture for the client

- [ ] Step 1 change set showing the single Add of `CfnServiceRole`
- [ ] Trust relationships of `iamr-sandbox-sbx-cfn`
- [ ] Security Hub showing `cfn-role-test` as Failed (before)
- [ ] Pipeline run summary: the Prepare *Service role* row and the Deploy result
- [ ] Stack info of `cfn-role-test` showing the IAM role
- [ ] Security Hub showing `cfn-role-test` as Passed (after)

### If something goes wrong

| What you see | What it means | What to do |
| --- | --- | --- |
| Step 1 change set changes `DeployRole` or `ReadRole` | The stack in AWS differs from the template | Do not execute; send a screenshot |
| Prepare warns *No CloudFormation service role yet* | Step 1 is not done, or the role has another name | Finish step 1, then run again |
| Prepare stops with *Can't check the service role* | The deploy role could not read the role in IAM | Send the run log |
| Change set says *No changes* and *Service role not attached yet* | The template still says `v1` | Change it to `v2`, commit, run again |
| Change set fails: role *is invalid or cannot be assumed* | The role does not trust `cloudformation.amazonaws.com` | Recheck step 1, item 7 |
| Draft PR shows other differences in the workflow | The repo's pipeline changed after 17 Nov 2026 | Send the repo file; the change is redone on top of it |
| Security Hub still shows Failed after the run | The check has not re-run yet | Confirm the stack's IAM role (step 3, item 6), then wait |

### How to undo

- Delete branch `test/cfn-service-role`. The pipeline on `main` never changed.
- Delete stack `cfn-role-test` if it is still there.
- The new role can stay: it does nothing unless a pipeline passes it. To remove it, delete it by hand in IAM. Taking it out of the template keeps it, on purpose.
- A stack that has used the role keeps it for good; that is why the test uses a throwaway stack.

After a passing test: merge the branch without the test folder, then roll out with the StackSet script and `cfn-service-role-changes.md`.
