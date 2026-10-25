
Here's the full list. You can get almost all of it during one real request for each pipeline, so there's no need to make test changes.

**GitHub screens** (take these once, during whichever change you do first; I'll use them in both docs)
- **G1** Repo home › click the branch button (it shows `main`) › type the new branch name. Capture it while "Create branch … from 'main'" is showing, before you click.
- **G2** After pasting your change › **Commit changes…** › capture the dialog with the message filled in and "Commit directly to the feat/… branch" selected.
- **G3** Repo home just after the commit, showing the yellow "had recent pushes" bar with **Compare & pull request**.
- **G4** The "Open a pull request" page: base `main` ← your branch, with the title, description and a reviewer filled in. Capture it before clicking Create.
- **G5** After approval, the bottom of the Pull Request's Conversation tab, showing the approval and the green **Merge pull request** button.
- **G6** After **Confirm merge**, the purple Merged badge and the **Delete branch** button.

**DNS doc** (use the next real record request)
- **D1** The zone template in the GitHub editor (pencil icon) on your branch. Scroll so the record above and your new block both show.
- **D2** The Pull Request's **Files changed** tab, with only your green record block.
- **D3** Actions › CloudFormation Stack using S3 as required › **Run workflow**, with every field filled in and Deploy = `no`. Capture it before clicking Run.
- **D4** The run's **Prepare** summary table (Stack, Account, Plan).
- **D5** The **Change set summary** showing 1 to add · 0 to modify · 0 to remove, with Property-level changes open. This replaces my CloudFront example.
- **D6** Click **Review and execute it in the AWS console** while signed in to the zone's account. Capture the change set page with the **Execute change set** button.
- **D7** Click Execute and capture the confirmation dialog, which shows the stack failure options, before you confirm. Tell me which option you choose.
- **D8** The stack's **Events** tab showing `UPDATE_COMPLETE`.
- **D9** Route 53 › Hosted zones › the zone, filtered to the new record.
- **D10** (optional) Your terminal showing the `nslookup` result.
- **D11** (optional) An error example: run again with a misspelt folderPath and Deploy = `no`. It fails before any AWS step. Capture the red error.

**Trust store doc** (next real request, in tst first, which is the safe place for the Apply = yes shots)
- **T1** `truststore/tst/truststore.pem` in the GitHub editor, scrolled to the end: the last existing `END CERTIFICATE` line and your new block.
- **T2** The Pull Request's **Files changed** tab, with only the green certificate block.
- **T3** Actions › CloudFront mTLS Trust Store Management › **Run workflow**, with env `tst`, the region and apply = `no`. Capture it before clicking Run.
- **T4** That run's page, with Apply skipped. My current screenshot shows your test branch; one run from `main` replaces it.
- **T5** The Apply = `yes` run's page, with **3 · Apply trust store** green.
- **T6** **3 · Apply trust store summary**, showing the "Apply — SUCCESS" block.
- **T7** (optional) The Apply step's log, with the "Trust store status: …" lines ending in `active`.
- **T8** (optional) S3 › the tst bucket › the `truststore/` folder listing the versioned bundles. This shows why a rollback is possible.
- **T9** (optional) The tst trust store in the CloudFront console, showing Active.
- **T10** (optional) An error example: on a throwaway branch, add a blank line inside `truststore.pem` and run from that branch with apply = `no`. Validate fails before any AWS step. Capture the red error and the validator's lines above it, then delete the branch.

Name each file by its code (G1.png, D5.png, …) and zip them like last time. Full-window screenshots are fine; I'll crop them.

**Answers I need**

Both docs
1. Who is "the pipeline owner" that teammates should go to, and how do they reach them (name, plus a Teams channel or email list)?
2. Who can approve Pull Requests? Does `main` need an approval before a merge?
3. What access does a new teammate need, and how do they request it? For example, write access to `plat-eng/AWS-Landing-Zone` (to create branches and run workflows), and which AWS SSO account and permission set they need to execute DNS change sets.
4. For prd, does a change need an approved CHG or RITM before it's executed or applied? Is there a change window?

DNS
5. Which zones are under CloudFormation so far, with the folderPath, stackName, region and roleAlias for each? I'll make a lookup table so nobody has to guess the stack name. What should the team do for a zone that hasn't been imported yet?
6. Which request types come up most often (for example ACM validation CNAMEs, TXT verification, an alias to a load balancer)? I'll add a snippet for each one.

Trust store
7. How do certificates usually arrive: pasted PEM text, a .cer or .crt file, or a zip with a chain? Do you add the root and intermediate CAs, or only the issuing CA?
8. What's the fewest days to expiry you'd accept for a new certificate?
9. When should someone choose us-east-1 instead of eu-west-1?
10. Please attach `validate-truststore.sh` so the troubleshooting table can quote its real messages.
11. Will you add the `truststore.pem` refresh fix before handing over? If so, I'll remove the known-issue warnings.

**What I'll add anyway** (tell me if you don't want any of these)
- **DNS: check the Plan row.** In the Prepare table, Plan must say "Exists … → drift check, then UPDATE change set". If it says "New stack → CREATE change set", the stackName is wrong, and executing it would create a second hosted zone with the same name. The doc will also say not to execute if the run shows a Replacement, Removal or "Older change sets waiting" warning.
- **DNS: changing an existing record.** A new value shows up as Modify. A new name replaces the record, and because records are set to Retain, the old name stays live.
- **Trust store: removing a certificate.** A short procedure where the report must show 0 added and 1 removed.
- **Both: a "Before you start" box** at the top, built from your answers to questions 1–4.