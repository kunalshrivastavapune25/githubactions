# Installing the CloudFormation Deploy Pipeline

1. Copy the `.github/` and `cloudformation/` folders from this package into the root of your CloudFormation repository. They add new files only. The existing `latest_cft_deployment.yml` keeps working until you delete it.
2. Commit, raise a PR, merge.
3. Follow **[.github/cfn-pipeline/README.md](.github/cfn-pipeline/README.md) → One-time setup**:
   * secrets `CFN_PIPELINE_SERVICES_ROLE` and (optional) `KIRO_API_KEY`
   * repository variables `CFN_MAIL_FROM` / `CFN_MAIL_TO`
   * optionally, the `cfn-change-approval` environment
4. Run **Actions → CloudFormation Deploy Pipeline → Run workflow**.

Nothing is deployed unless someone executes the change set. Your first runs are safe even before email or Amazon Q is configured.
