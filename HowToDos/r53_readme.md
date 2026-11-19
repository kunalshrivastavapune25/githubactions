Runbook: Bring existing Route 53 records under CloudFormation
Nov 19, 2026 · @Kunal Shrivastava
Purpose and scope
Use this runbook to put the hand-made records in a network services prod hosted zone into that zone's CloudFormation stack, one zone at a time, without changing any DNS answer. It applies to each of the roughly 75 hosted zones, whenever a zone is due.
The script r53_export.py checks CloudFormation first and exports only what no stack manages yet. Once a zone is done, its YAML lives in GitHub and every DNS change goes through the GitHub Actions pipeline.
Before you start
You can sign in to network services prod with admin access through SSO.
You have Python 3.8 or later with boto3 and PyYAML, and a copy of r53_export.py.
The first zone is the pilot: test.tescoinsurance.com. Prod zones follow one at a time, after it comes through cleanly.
Section 1: Quick steps for developers
Each zone takes one run of the script and one pipeline deployment. A zone with no stack needs an import first.
Sign in and set up. Run aws sso login --profile <profile>, then export AWS_PROFILE=<profile> AWS_DEFAULT_REGION=<stack region> and pip install boto3 pyyaml.
Run the script. Run python3 r53_export.py <zone name or zone ID>. Use the zone ID when a public and a private zone share a name. The script prints what it found, what it left out and why.
No stack yet? The script writes <zone>-hostedzone.yaml and <zone>-import.json, and prints two commands. Run the first, review the import change set in the console, then run the second. IaC generator works too. Then go back to step 2.
Add the records. The script writes <zone>-records.yaml with only the records no stack manages yet. Copy every block under its Resources: line to the end of the Resources: section in the zone's template in GitHub. If that template isn't in GitHub yet, run the script with --full to get the whole template with the records already added.
Deploy. Raise a PR, merge it after approval, and run the pipeline. In the change set, every row must read Add and AWS::Route53::RecordSet, and the count must match what the script printed. Execute it, leaving the rollback options at their defaults: never choose to delete all newly created resources during a rollback.
Check. Run step 2 again. It should say "Nothing to export: every record is already in a stack". Then run drift detection on the stack: no record should show as MODIFIED or DELETED.
For the pilot zone, also open CloudTrail Event history and filter on the event name ChangeResourceRecordSets. Each successful call from cloudformation.amazonaws.com should hold a DELETE and a CREATE of the same record. Start the prod zones only after that.
For a big zone, run the script with --max 50, deploy those records, then run it again for the next 50. If the script says the template is over 51,200 bytes, the pipeline has to deploy it from S3.
Section 1: Troubleshooting
None of these changes live DNS. Every record carries Retain, and nothing runs until someone executes a change set they have reviewed.
What you see
What it means
What to do
already exists on a record, then a rollback
The YAML differs from the live record, often because someone changed the record after the export.
Nothing to undo: the record stays live. Run the script again, swap in the fresh block for that record, and redeploy.
Rate exceeded, Throttling or PriorRequestNotComplete
Route 53 throttled the update.
Run the script with --max 20 and deploy the records in smaller PRs.
Over 51,200 bytes, deploy it from S3
The zone's template is too big to pass inline.
Deploy it from S3 with --template-url. The pipeline needs to do the same.
over the 500 per stack
The zone has more than about 490 records.
Stop. The zone's records need two stacks, so agree the split with the platform team first.
No CloudFormation stack holds … yet
The zone has not been imported into a stack.
Run the two commands the script prints, or use IaC generator, then run the script again.
Run again with the zone ID
A public and a private zone share the name.
Run the script again with the zone ID it lists.
is in more than one stack
Two stacks claim the zone.
Keep the zone in one stack only before going on.
managed by servicediscovery.amazonaws.com
Cloud Map owns the zone.
Leave the zone alone.
left out … already managed by stack …
Another stack already owns that record.
Nothing. The record stays with that stack.
check … ExternalDNS or has settings this script does not copy
Another tool may own the name, or the record uses a setting the script does not copy.
Agree a plan with the owner. To keep the record out, delete its block before you deploy.
could not check … region(s), usually blocked by an SCP
Those regions are closed to your role.
Nothing, as long as no DNS stacks live there.
The change set shows Modify or Remove
The deployed file differs from the stack, for example the records file deployed on its own.
Don't execute it. Fix the YAML in GitHub and run the pipeline again.
Section 2: Reference guide for non-developers
The script copies each record into YAML exactly as Route 53 holds it. Once that YAML is deployed to the zone's stack, CloudFormation takes over each record without changing what DNS answers.
What changes
Today the records are made by hand in the console, and nothing records who changed what or why. After the move, each record is a block in the zone's YAML in GitHub and a resource in the zone's stack. Every change then goes through a PR review and a change set that someone checks before it runs.
Why CloudFormation import is not used
CloudFormation can import existing resources, but for DNS records it asks for a SetIdentifier. Only records with a routing policy (such as weighted, latency or failover) have one, and most of ours are simple records. The workaround, turning each record into a weighted one for the import, would rewrite every live prod record twice, so we don't use it.
How a record is taken over
[embedded content: how CloudFormation takes over a record · 1 check, 2 outcomes]
When the template matches, DNS doesn't change, because the delete and the re-create go to Route 53 in one call. When it doesn't match, the update stops before the live record is touched.
Safety nets
Every record carries DeletionPolicy and UpdateReplacePolicy Retain, so a rollback or a stack deletion never deletes a live record. Someone who skipped this lost the records they were moving when one record failed.
The script leaves out the zone's own NS and SOA records, records any stack already owns, traffic-policy records and Cloud Map zones.
Nothing runs until someone has reviewed the change set, which must show only Add rows for AWS::Route53::RecordSet.
--max sends a big zone in smaller updates, so a failure rolls back less.
Running the script again is the check: once every record is in a stack, it exports nothing.
Section 2: After adoption and known limits
Once a zone is adopted, its records change only through GitHub: edit the zone's YAML, raise a PR, run the pipeline, review the change set, then execute it.
To do this
Change in the YAML
What happens in Route 53
Add a record
Copy a record block, give it a new logical ID, and set its Name, Type and values.
CloudFormation creates the record.
Change a record
Edit its values, TTL or alias target.
CloudFormation updates the live record.
Remove a record
Delete its block.
With DeletionPolicy Retain, the record only leaves the stack. It stays live until someone deletes it in Route 53.
To make a deletion in Git remove the DNS record, switch the records (not the hosted zone) to DeletionPolicy: Delete. Make that switch in the same PR as a real change to the zone.
Open question: does the team want deletions in Git to remove DNS records, or keep Retain everywhere?
Known limits
AWS does not document that CloudFormation takes over records that already exist. People have seen it work, and someone who says they work at AWS Support suggested it, so always review the change set, and check the pilot zone in CloudTrail as Section 1 describes.
Wildcard names appear as \052, which is how Route 53 writes *. This is on purpose: the template keeps Route 53's exact text.
The zone's own NS and SOA records, records made by a traffic policy, and Cloud Map zones are never exported.
A template over 51,200 bytes must be deployed from S3, and one stack holds at most 500 resources.
A record changed in the console after adoption shows up as drift. Update the YAML to match, or undo the console change.
Sources
AWS CDK CLI issue 2001: import requires SetIdentifier for AWS::Route53::RecordSet
CloudFormation coverage roadmap issue 560: RecordSet import and taking over identical records
AWS::Route53::RecordSet, CloudFormation template reference
DeletionPolicy attribute, CloudFormation template reference