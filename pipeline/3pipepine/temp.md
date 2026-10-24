please find attached 3 pipelines we are using in my project

1. old-pipeline-firewall.yml

2. latest_cft_deployment.yml

3.1  ws2-dns-records-apply.yml

3.2 ws2-dns-records-export.yml


I've updated the deck; it's now 12 slides.

- **WS2's missing approval gate:**
  - **Slide 6:** the WS2 Apply flow now has a red "MISSING" Approval step.
  - **Slide 7:** lists this gap first.
  - **Slide 8:** the comparison table has a new "Pause for approval" row.
  - The point: the apply file says outside sandbox a gatekeeper must approve, but that job was never added. So ticking "execute" changes live DNS straight away, production included. The export only reads DNS, yet it does have an approval step; that contrast is in the speaker notes.
- **Backup point:** new slide 9, "When DNS breaks: find it, undo it".
  - **WS2 export:** someone has to run it by hand, and the copy sits on a side branch or in a download kept 30 days. It leaves out alias records, and restoring through Apply has no approval step.
  - **New pipeline:** the real record templates are in Git, showing what changed, who approved it and when. To undo, you revert the commit, check the change set and click Execute.
  - The table also has a "Backup and rollback" row.
- **Screenshot boxes:** each one now has grey instructions inside, shown only while you're editing. Click the box and press Ctrl+V; the text disappears once the picture is in.

**What to paste where**
- **Slide 5** (all four from a sandbox run of the new pipeline):
  1. GitHub › Actions › "CloudFormation Stack using S3 as required" › a run › the four-box job graph.
  2. Change a tag by hand on a sandbox stack resource, then run the pipeline. It stops at Drift check; crop the run's "Drift found" table and the expected/actual lines. Undo the tag afterwards.
  3. A run with a real change and Deploy = no › the run summary's "Change set ready for review" section, with its counts and table.
  4. Click "Review and execute it in the AWS console" › the change set page showing the list of changes and the "Execute change set" button.
- **Slide 7:** ws2-dns-records-apply.yml in GitHub, lines 418–444 in the copy you sent (click line 418, then Shift+click 444). It shows the approval heading with no job under it, and `apply` waiting only on `[validate-account, preview]`. Take this from the code; don't run a production apply to prove it.
- **Slide 9:** your DNS record template in GitHub › History, or a merged Pull Request › Files changed, showing one record before and after.
- **Slide 10:** GitHub › Actions › "Old Pipeline for Firewall Standard Change" › any run › the single "deploy" job.
- **Slide 11:** the AWS ["Resource type support" page](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/resource-import-supported-resources.html). Search for "AWS::FMS::Policy" and crop the header row plus that row, which says Drift detection: No.

On slide 9 I wrote that Git lets you "see what changed" rather than "catch drift". AWS's drift check doesn't cover individual DNS records, and changes made by hand in the AWS console never reach Git, so a drift claim there could be knocked down. A footnote on the slide says this and recommends making the pipeline the only way DNS gets changed.



I've added slide 10, "What each backup actually holds". It puts two screenshot boxes side by side, with a short list under each:

- **WS2 export (JSON written by a Python script):** ordinary records only. The hosted zone itself isn't in the file, and SOA, the zone's own NS records and alias records are skipped.
- **New pipeline (CloudFormation YAML in Git):** the hosted zone itself, every record as its own resource, and alias records too. Each zone is one stack.

**What to paste**
- **Left box:** the WS2 export Pull Request › Files changed › `config/accounts/<account>/networking/dns-records-<zone>.json`. Crop the start of the file so it's clear there's no hosted zone, SOA or NS in it.
- **Right box:** the same zone's CloudFormation template in GitHub. Crop the top: the `AWS::Route53::HostedZone` resource, then the first `AWS::Route53::RecordSet` resources, ideally including an alias record.

**Tips**
- Use the same zone on both sides, e.g. tescoforbusiness.com, since its template is already done.
- To get the JSON for that zone, run a WS2 export for just that one zone. It only reads DNS and needs no approval, but it opens a branch and a Pull Request, which you can close afterwards.
- If the WS2 developer pushes back, the export run's own summary has a "Record Export Behavior" table that lists SOA, Apex NS and ALIAS as SKIPPED. That's in the speaker notes.
- The old-pipeline and AWS docs screenshots have moved to **slides 11 and 12**. The cross-references on slides 4 and 8 are updated.

The YAML doesn't list SOA and NS as records either, because Route 53 creates them with the zone. So the slide says they "come with" the zone rather than "are in the file". The real difference is that your template defines the zone and WS2's file doesn't, which can't be argued with.



Both drafts are ready. The client email comes in two versions:

- **"Save details for the call"** keeps the WS2 approval-gate finding for the meeting. It's the safer choice if the WS2 developer is on the email.
- **"Flag the approval gap"** raises the gate in writing, as a small fix you're offering to help with. It's worth asking Raju which one he prefers.

If you've already pasted the screenshots, delete that line from Raju's email.




Subject: Please review: deployment pipelines deck for the client call

Hi Raju,

I've attached the deck comparing our three deployment pipelines, which I'd like us to present to the client together. Could you please review it and let me know any changes?

What it covers:
• New pipeline (latest_cft_deployment.yml): a drift check and a change set on every run. By default, nothing goes live until someone clicks Execute in AWS.
• WS2 DNS pipelines (built by the client's SME): good for bulk DNS record edits, but there's no drift check or change set. The approval step described in the apply workflow was never actually added, so ticking 'execute' changes live DNS straight away, production included.
• Backup and rollback: WS2's export is a partial snapshot (no hosted zone, SOA/NS or alias records). Our CloudFormation templates in Git hold the whole zone, and we can undo a change through a reviewed change set.
• Old pipeline: kept only for FMS policies and rule groups, where AWS's drift check can't help.

Recommendation: make the new pipeline the standard, keep the old pipeline for those exceptions, and move DNS record changes onto the new pipeline.

Since the client's SME built WS2, I've kept the tone factual and credited what it does well. Please tell me if any slide comes across too strongly.

The screenshot boxes are marked in the deck, and I'll fill them from our sandbox runs before we share it.

Once you're happy with it, could we pick a time for the client call?

Thanks,
Kunal


Subject: Deployment pipelines review – deck ahead of our call

Hi all,

Please find attached a short deck comparing the three pipelines we use to make changes in AWS: the new CloudFormation pipeline, the WS2 DNS pipelines and the existing firewall pipeline.

It focuses on two checks we believe every change should pass:
• Drift check – has anything been changed by hand since the last deployment?
• Change set – an itemised list of exactly what will change, reviewed by a person before it goes live.

It also covers how we can quickly find and undo a DNS change if there's a production issue. It lists the few resource types that AWS can't drift-check (Firewall Manager policies and rule groups), which would stay on the existing pipeline.

Our recommendation is to make the new pipeline the standard way to change AWS, keep the existing pipeline only for those exceptions, and move DNS record changes onto the new pipeline as well.

Raju and I would be glad to walk you through it on a short call. Please let us know a time that suits you this week.

Kind regards,
Kunal Shrivastava
Platform Engineering