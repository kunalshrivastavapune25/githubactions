This takes about 30–40 minutes and gets you 15 screenshots for both decks. You start the slow pipeline runs first and take the other screenshots while they finish. Press Win+Shift+S and drag a box to take each one; Windows saves them in Pictures › Screenshots. Crop out account IDs if you'd rather not share them here; the slides work either way.

**Step 0 – Get ready**
Sign in to GitHub. In the same browser, sign in to the AWS console for the **sandbox** and **tst network** accounts.

**Step 1 – Add a small test stack on a new branch** (sandbox only, deleted at the end)
- In the repo that has the new pipeline, open the branch dropdown, type `pipeline-demo` and create the branch from main.
- Add file › Create new file › `cloudformation/pipeline-demo/pipeline-demo-template.yaml`:
```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Pipeline demo - one small SQS queue to show the drift check and change set. Safe to delete.

Parameters:
  VisibilityTimeout:
    Type: Number
    Default: 30
    Description: Seconds a message stays hidden after it is read

Resources:
  DemoQueue:
    Type: AWS::SQS::Queue
    Properties:
      QueueName: !Sub '${AWS::StackName}-queue'
      VisibilityTimeout: !Ref VisibilityTimeout
      SqsManagedSseEnabled: true
      Tags:
        - Key: Purpose
          Value: pipeline-demo

Outputs:
  QueueUrl:
    Value: !Ref DemoQueue
```

- Add a second file, `cloudformation/pipeline-demo/params.json`:
```json
[
  { "ParameterKey": "VisibilityTimeout", "ParameterValue": "30" }
]
```
- Commit both files to `pipeline-demo`.

**Step 2 – Create the test stack (run A)**
Go to Actions › "CloudFormation Stack using S3 as required" › Run workflow. Choose branch `pipeline-demo`, folderPath `pipeline-demo`, stackName `pipeline-demo`, roleAlias `SANDBOX`, region `eu-west-1`, Deploy `yes`. No screenshot is needed here, so carry on while it runs.

**Step 3 – Start the trust store run**
Go to Actions › "CloudFront mTLS Trust Store Management" › Run workflow from main. Choose env `tst`, your usual region, and Apply `no`.

**Step 4 – Screenshots that don't need a run** (take these while the runs finish)
- 📸 **Shot 1** (review deck, slide 7): in the WS2 repo, open `ws2-dns-records-apply.yml`. Click line 418, Shift+click line 444, and capture the highlighted lines.
- 📸 **Shot 2** (review deck, slide 9): open your tescoforbusiness.com template, click **History**, and capture the list of commits.
- 📸 **Shot 3** (review deck, slide 10, right): the same template, top of the file. Capture `AWS::Route53::HostedZone` and the first record sets, including an `AliasTarget` record if one fits.
- 📸 **Shot 4** (review deck, slide 10, left): in the WS2 repo, press `t`, type `dns-records-`, and open a zone's `.json` file (the same zone, if there is one). Capture the top of the file.
  - If no such file exists, run WS2 Export for one zone in sandbox. Open the Pull Request it creates › Files changed, take the screenshot, then close the Pull Request.
- 📸 **Shot 5** (review deck, slide 11): Actions › "Old Pipeline for Firewall Standard Change" › any past run. Capture the single "deploy" job.
- 📸 **Shot 6** (trust store deck, slide 3): the `…/ims-app-identity-main/truststore` folder, showing tst, ppe and prd.

**Step 5 – Trust store run results**
- 📸 **Shot 7** (trust store deck, slide 4): the run page with its job graph.
- 📸 **Shot 8** (trust store deck, slide 5): the run's Summary › "Certificate comparison". Capture the inventory table, the first rows of the certificate tables, and the Summary lines. Two screenshots are fine here.

**Step 6 – Trust store apply** (only if Shot 8 says "No certificate changes")
- Run it again with Apply `yes`. This puts back the same certificates, so tst doesn't change.
- 📸 **Shot 9** (trust store deck, slide 6, top): the run's Summary › "Apply — SUCCESS".
- 📸 **Shot 10** (trust store deck, slide 6, bottom): AWS console (tst) › S3 › the trust store bucket › `truststore/` folder, showing the list of `truststore-….pem` files.

If the comparison shows changes, skip this step and tell me. I'll replace those two boxes with a short text panel.

**Step 7 – Change set (run B)**
- On the `pipeline-demo` branch, edit `params.json` to change `"30"` to `"60"`, and commit.
- Run the pipeline again with the same inputs, but Deploy `no`.
- 📸 **Shot 11** (review deck, slide 5, box 1): the job graph. Prepare, Drift check and Change set should be green, and Deploy grey.
- 📸 **Shot 12** (review deck, slide 5, box 3): the run's Summary › "Change set ready for review", with the counts and the table.

**Step 8 – Review and execute**
- In that Summary, click "Review and execute it in the AWS console". You need to be signed in to sandbox.
- 📸 **Shot 13** (review deck, slide 5, box 4): the change set page, showing the list of changes and the **Execute change set** button.
- Click Execute change set and wait until the stack shows UPDATE_COMPLETE.

**Step 9 – Drift (run C)**
- AWS console (sandbox) › SQS › `pipeline-demo-queue` › Edit. Change Visibility timeout to `45` seconds and save.
- Run the pipeline again with the same inputs and Deploy `no`. It should stop at Drift check.
- 📸 **Shot 14** (review deck, slide 5, box 2): the run's Summary, showing "Drift found", the table, and the expected / actual lines.

**Step 10 – AWS docs** (any device, even at home)
- Open [Resource type support](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/resource-import-supported-resources.html) and press Ctrl+F for `AWS::FMS::Policy`.
- 📸 **Shot 15** (review deck, slide 12): capture that row. Then scroll up and take a second screenshot of the table's header row; I'll stack the two.

**Step 11 – Clean up**
In CloudFormation (sandbox), delete the `pipeline-demo` stack. Then delete the `pipeline-demo` branch.

**Step 12 – Send them here**
Attach the screenshots and tell me each one's shot number. I'll fit them into the boxes and send back both final decks.

If run A fails, for example because the sandbox role can't create SQS queues or a tagging rule blocks it, send me the error and I'll change the test template.