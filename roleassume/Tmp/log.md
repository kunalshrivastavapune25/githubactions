I'll pull AWS's current console steps for a service-managed StackSet, so the UI instructions match the screens you'll see.

I have AWS's current screen names. I'll generate the account ID lists straight from the template so you can paste them without typos.

Here's the YAML (`platform-iam-roles-stackset-template.yml`, the same 14-account file as before) and the console steps. You can ignore the script.

**Before you start:** in AWS Organizations (IMS Root), open AWS accounts and copy the OU IDs (`ou-xxxx-xxxxxxxx`) of **Infrastructure** and **Security**. The Infrastructure ppe and dev sub-OUs are included automatically.

**Console steps (IMS Root 390403857119)**
1. Open **CloudFormation** and set the Region (top right) to **Europe (Ireland) eu-west-1**.
2. Go to **StackSets**, then **Create StackSet**.
3. **Permissions:** Service-managed permissions. If a banner says trusted access is off, it is activated when you click Next in step 6.
4. **Prerequisite:** Template is ready. **Specify template:** Upload a template file, choose `platform-iam-roles-stackset-template.yml`, then **Next**.
5. **Specify StackSet details:**
   - Name: `ims-platform-iamr-roles`
   - Parameter `SourceAccountId`: leave the pre-filled `390403857119`
   - **Next**
6. **Configure StackSet options:**
   - Execution configuration: **Active**
   - Capabilities: tick **I acknowledge that this template may create IAM resources**
   - **Next**
7. **Set deployment options:**
   - **Deployment targets:** Deploy to organizational units (OUs). Add an OU: Infrastructure OU ID, then add an OU: Security OU ID.
   - **Account filter type:** Intersection. Paste the account IDs (lists below).
   - **Automatic deployment:** Deactivated.
   - **Specify regions:** **Europe (Ireland) only.** IAM is global, so a second Region would fail on the role names.
   - **Deployment options:** Maximum concurrent accounts: Number **5**. Failure tolerance: Number **14**, so every account is tried even if one fails. Region concurrency: Sequential. Concurrency mode: Strict failure tolerance.
   - **Next**
8. **Review:** check it shows 2 OUs, Intersection with your accounts, and 1 Region. Then **Create StackSet**.
9. **Check the result:** on the StackSet page, **Operations** should show SUCCEEDED and **Stack instances** should show each account as CURRENT. If an account shows OUTDATED, its status reason says why.

**Account IDs to paste in step 7**
- All 14 at once:
  `356666487118,899867382346,116355921084,794035561643,119810927880,502818572771,117837574201,050916357979,277352717275,575210791269,119328599021,697858908364,084828582217,816069122659`
- Or pilot first: `697858908364` (Shared_Services dev). Once it shows CURRENT, add the other 13 via **Actions**, then **Add stacks to StackSet**. Use the same two OUs, Intersection and Europe (Ireland):
  `356666487118,899867382346,116355921084,794035561643,119810927880,502818572771,117837574201,050916357979,277352717275,575210791269,119328599021,084828582217,816069122659`

Paste the IDs rather than typing them: two start with a zero (`050916357979`, `084828582217`).

**Afterwards:** re-run **Test account setups** for the Infrastructure and Security OUs. Those 28 red rows should turn green.

Source: [Create StackSets with service-managed permissions (AWS docs)](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/account-level-targets.html)
