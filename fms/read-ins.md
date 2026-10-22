

`fms_deploy.py` now checks drift. It compares every live policy in Firewall Manager with the template the stack was last deployed with, and lists anything changed in the console. When you also pass your YAML, each change says what applying your file will do to it.

```bash
# only the manual changes
python3 fms_deploy.py --stack-name <your-fms-stack> --drift-only

# manual changes + what your file does to each one
python3 fms_deploy.py --stack-name <your-fms-stack> --template ims-fms-policies.yml --drift-only

# a normal run shows the same report first, then the change set and the yes/no prompt
python3 fms_deploy.py --stack-name <your-fms-stack> --template ims-fms-policies.yml
```

This is from a simulated run, with someone's console edits on top of your challenge→count file (paths shortened):

```
FMSPolicy...001 ("IMS-test-WAF-policy"): changed outside CloudFormation
  + ...[AWSManagedRulesBotControlRuleSet].excludeRules[TGT_TokenReuseIp]: added in live {"name": "TGT_TokenReuseIp"}
      your file doesn't have it -> applying it REVERTS this change
  ~ ...ruleActionOverrides[TGT_VolumetricSession].actionToUse: deployed {"challenge": {}}, live {"captcha": {}}
      your file sets {"count": {}} -> applying it replaces this change
FMSPolicy...003 ("IMS-prd-api-waf-policy"): changed outside CloudFormation
  ~ ...ruleActionOverrides[Host_localhost_HEADER].actionToUse: deployed {"block": {}}, live {"count": {}}
  Your file doesn't change this policy, so applying it leaves these changes live.
FMSPolicy...005 ("IMS-ppe-web-waf-policy"): DELETED in Firewall Manager; the stack still lists it
In no CloudFormation stack: "IMS-adhoc-shield" (...), created outside CloudFormation

5 manual change(s) in 3 of 13 policies, 1 policy deleted; applying your file reverts 2 of them.
```

- **Policies your file doesn't change:** CloudFormation won't touch them, so their console changes stay live. The stack's template still holds the old values for them.
- **Policies your file changes:** CloudFormation sends your file's whole policy to Firewall Manager. Any console change in that policy that isn't in your file is reverted. To keep one, copy the live value shown into your file; it then shows as "kept".
- **When applying:** the number of reverts appears as a warning in the yes/no prompt, and `--strict` refuses to continue. Editing a policy that was deleted in the console is blocked.
- **Also listed:** policies created in the console that aren't in any stack.
- **In a pipeline:** `--drift-only` exits with code 2 when it finds manual changes.

Replace your `fms_cfn_guard.py` as well, because the report depends on changes in it. The pipeline snippet is down to one step: `fms_deploy.py` prints the manual changes first, then creates and checks the change set for you to execute in the console. The pipeline role now also needs `cloudformation:DescribeStackResources`.

I tested this on the simulated 13-policy stack, not your account. It covers console changes in a policy your file edits and in one it doesn't, plus a deleted policy and policies outside the stack. After applying, the edited policy matched your file and the other console change was still there.

I'll run the same simulated scenario through the apply step and show you the output.

This is the third command on the same simulated stack, with the same console edits, and the answer at the prompt was yes. I shortened the IDs and paths and collapsed the manual-changes report you've already seen. The timings are simulated.

```
$ python3 fms_deploy.py --stack-name ims-fms-policies --template ims-fms-policies.yml
Manual changes: the live policies in Firewall Manager vs. the template ims-fms-policies was last deployed with
  [... same report as the drift run ...]
5 manual change(s) in 3 of 13 policies, 1 policy deleted; applying your file reverts 2 of them.

Created change set fms-deploy-20261022-040732; waiting for CloudFormation to work out the changes...
Change set fms-deploy-20261022-040732 on ims-fms-policies (CREATE_COMPLETE): 1 resource change(s)
  Modify  FMSPolicy...001  AWS::FMS::Policy  Replacement: False

FMSPolicy...001 ("IMS-test-WAF-policy"): updated in place, deployed template -> new template
  ~ ...[AWSManagedRulesBotControlRuleSet].ruleActionOverrides[TGT_VolumetricSession].actionToUse: {"challenge": {}} -> {"count": {}}

WARNING  Applying ims-fms-policies.yml reverts 2 change(s) made outside CloudFormation (marked REVERTS under "Manual changes" above).

OK: 1 FMS policy update(s) in place, 0 new; none deleted or replaced.

Apply change set fms-deploy-20261022-040732 to ims-fms-policies (1 warning(s) above)? Type yes to apply: yes

Applying fms-deploy-20261022-040732 to ims-fms-policies (event times in UTC):
  04:07:55  ims-fms-policies  UPDATE_IN_PROGRESS                   User Initiated
  04:07:58  FMSPolicy...001   UPDATE_IN_PROGRESS
  04:08:01  FMSPolicy...001   UPDATE_COMPLETE
  04:08:04  ims-fms-policies  UPDATE_COMPLETE_CLEANUP_IN_PROGRESS
  04:08:07  ims-fms-policies  UPDATE_COMPLETE

Live check: FMSPolicy...001 ("IMS-test-WAF-policy") now matches the template.
Firewall Manager now rolls the change out to the member accounts over the next few minutes.
Done: ims-fms-policies is UPDATE_COMPLETE.
```

- **What to check before typing yes:** there should be exactly one `Modify` line, and it should say `Replacement: False`. That means the policy is updated in place and the other 12 policies aren't in the change set. Under it is the only change, challenge → count. In the simulation, Firewall Manager received one update, for IMS-test-WAF-policy only.
- **The warning:** in the console, someone excluded TGT_TokenReuseIp and set the default action to BLOCK, both in this same policy. Your file has neither, so applying it removes them. To keep them, copy them into your file first and the warning goes away. If nothing was changed in the console, the report is one line ("No manual changes in the 13 live policies…") and the prompt doesn't mention warnings.
- **After yes:** the script shows the stack events as they happen. Then it reads the policy back from Firewall Manager and confirms it matches your file. A `--drift-only` run right after this no longer lists IMS-test-WAF-policy. It still shows the console changes in the two policies your file didn't touch: `2 manual change(s) in 2 of 13 policies, 1 policy deleted.`
- **Any answer other than yes:** the change set is deleted and nothing is applied. To execute from the console instead, as you did for Route 53, add `--keep-change-set` and answer no. The change set then stays on the stack.
- **If the update fails:** CloudFormation rolls back and the policies stay as they were. The script prints the `UPDATE_FAILED` event with its reason, then `UPDATE_ROLLBACK_COMPLETE`, and exits with code 1.