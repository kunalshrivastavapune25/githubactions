i have a public hosted zone named tescoforbusiness.com in this account, it contains 26 records all created manually i want u to use apis to generate ymls correcpomtding to all these live resources, please maike a clouf formation template yml along with params file , so that i can run command in cloudshell there i will use this yml and param file to create a new channge set for import once this is done i will use ui to see new stack with review in progress and then if i finf all good in chnageset i will execute same from ui



I'll build a Python script that reads the zone live and writes the import template, parameters file and the resources-to-import file the CLI needs, then test it before sending it over.

Heads-up before you run anything: CloudFormation can't **IMPORT** `AWS::Route53::RecordSet`. It isn't on the import-supported list (only the hosted zone itself is), so an IMPORT change set with your records would be rejected.

What does work is a normal **CREATE** change set whose records match the live ones exactly. CloudFormation then takes over each existing record instead of failing, with no DNS change. Your flow stays the same: new stack in REVIEW_IN_PROGRESS → review → Execute in the console.

I'm building the script around that. It copies values verbatim, puts `DeletionPolicy: Retain` on every record and writes a JSON backup of the zone first.

`r53_zone_to_cfn.py` reads the zone live and writes the template, the params file and a JSON backup of the zone. It then prints the exact commands with your file names. Upload it to CloudShell (Actions → Upload file) in the account that owns the zone, then run:

```bash
python3 r53_zone_to_cfn.py --zone-name tescoforbusiness.com

aws cloudformation create-change-set \
  --stack-name tescoforbusiness-com-dns-records \
  --change-set-name adopt-existing-records \
  --change-set-type CREATE \
  --template-body file://tescoforbusiness-com-records.yml \
  --parameters file://tescoforbusiness-com-records-params.json
```

The one change to your plan is `CREATE` instead of `IMPORT`. Record sets aren't on CloudFormation's import-supported list. But when a template matches an existing record exactly, CloudFormation takes that record over on create, and AWS Support has suggested this as the workaround. That's why the script copies every value exactly as Route 53 returns it. The console flow is as you described: the stack sits in REVIEW_IN_PROGRESS, each record shows as Add, and you execute from the UI.

- **Count:** expect 24 resources, not 26. The console count includes the zone's own NS and SOA records, which stay with the hosted zone.
- **Safety:** every record has `DeletionPolicy: Retain`. AWS describes RetainExceptOnCreate as the option that deletes a resource when the operation that created it rolls back. So plain Retain keeps your records even if the run fails. A record that doesn't match fails on its own and stays as it was. Step 5 of the printed output compares the zone against the backup afterwards.
- **Optional trial run:** this behaviour isn't officially documented and it's a client's live zone. You can first build a one-record stack with `--only '^www\.'` (any regex on the record name). Delete that stack afterwards (the record stays), then run the full one.
- **The zone itself:** `--zone-import` also writes files to import the hosted zone into its own stack with a real IMPORT change set.
- **After takeover:** drift detection doesn't check record sets, so make future changes through the stack, not the console.

I tested it against a 37-record sample zone (aliases, TXT/DKIM quoting, wildcard, weighted/geo/failover records) and a simulated AWS API. Every generated record matched the source values exactly. I couldn't install cfn-lint here, so the script prints it as an optional check to run in CloudShell.

Sources:
- [AWS::Route53::RecordSet import issue #560 (cloudformation-coverage-roadmap)](https://github.com/aws-cloudformation/cloudformation-coverage-roadmap/issues/560)
- [CloudFormation resource type support](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/resource-import-supported-resources.html)
- [DeletionPolicy attribute](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-deletionpolicy.html)
- [Are RecordSets tracked by CloudFormation? (re:Post)](https://repost.aws/questions/QUbalNckJmRgSajeWFFSqdRg/are-recordsets-tracked-by-cloudformation)
- [CfnRecordSet GeoProximityLocationProperty (CDK docs)](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_route53.CfnRecordSet.GeoProximityLocationProperty.html)