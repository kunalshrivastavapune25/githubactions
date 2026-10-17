Worked for 12s

Understood. Let's strip this down to the essentials: one manually triggered GitHub Actions YAML, simple step-by-step logs, drift detection, and change-set creation only. No AI, no email, no gatekeeper, no deployment.

The behavior will be:

* Fresh stack: Skip drift detection, log that the stack doesn't exist, create a `CREATE` change set, print its details, and stop.

* Existing stack with drift: Print the drifted resources and stop without creating a change set.

* Existing stack in sync: Print the drift result, create an `UPDATE` change set, print its details, and stop.

* Any validation or AWS error: Fail the workflow with a useful log message.

The YAML will use your existing role-alias secrets and manual inputs wherever possible, rather than introducing a new approval or notification setup.

### Simple YAML to use

Save this as `.github/workflows/cfn-drift-and-change-set.yml`. It uses the existing input names and role-secret mapping from the workflow we've been reviewing. It does not add GitHub variables or secrets for email, AI, or approvals.

```yaml
name: CloudFormation - Drift and Change Set Only

on:
  workflow_dispatch:
    inputs:
      folderPath:
        description: "Folder under cloudformation/"
        required: true
        type: string
      stackName:
        description: "CloudFormation stack name"
        required: true
        type: string
      roleAlias:
        description: "Existing AWS role alias"
        required: true
        type: choice
        options:
          - NETWORK
          - DIRECTORY
          - SECURITY
          - SHAREDSERVICES
          - MANAGEMENT
          - LOGARCHIVE
          - COREAUDIT
          - MONITORING
          - DIRECTORY-DEV
          - DIRECTORY-PPE
          - MONITORING-PPE
          - SHAREDSERVICES-DEV
          - SHAREDSERVICES-PPE
          - SANDBOX
          - TST-NETWORK
          - PPE-NETWORK
          - BACKUP
          - CRV
      AWS_REGION:
        description: "AWS Region"
        required: true
        type: string
        default: eu-west-1

permissions:
  contents: read
  id-token: write

concurrency:
  group: cfn-${{ inputs.AWS_REGION }}-${{ inputs.stackName }}
  cancel-in-progress: false

jobs:
  drift-and-change-set:
    name: Drift Check and Change Set
    runs-on: ubuntu-latest

    steps:
      - name: 1 - Checkout code
        uses: actions/checkout@v4

      - name: 2 - Find template and parameters
        id: files
        shell: bash
        env:
          FOLDER_PATH: ${{ inputs.folderPath }}
        run: |
          set -euo pipefail

          case "$FOLDER_PATH" in
            /*|*..*)
              echo "ERROR: Invalid folder path"
              exit 1
              ;;
          esac

          DIR="cloudformation/$FOLDER_PATH"

          if [[ ! -d "$DIR" ]]; then
            echo "ERROR: Folder not found: $DIR"
            exit 1
          fi

          mapfile -t TEMPLATES < <(
            find "$DIR" -maxdepth 1 -type f \
              \( -iname '*-template.yaml' \
              -o -iname '*-template.yml' \
              -o -iname '*.template.yaml' \
              -o -iname '*.template.yml' \)
          )

          if [[ ${#TEMPLATES[@]} -ne 1 ]]; then
            echo "ERROR: Expected exactly one template; found ${#TEMPLATES[@]}"
            exit 1
          fi

          TEMPLATE="${TEMPLATES[0]}"
          PARAMS="$DIR/params.json"

          if [[ ! -f "$PARAMS" ]]; then
            echo "ERROR: params.json not found"
            exit 1
          fi

          jq -e 'type == "array" and
            all(.[]; has("ParameterKey") and has("ParameterValue"))' \
            "$PARAMS" >/dev/null

          echo "TEMPLATE=$TEMPLATE" >> "$GITHUB_ENV"
          echo "PARAMS=$PARAMS" >> "$GITHUB_ENV"

          echo "Template: $TEMPLATE"
          echo "Parameters: $PARAMS"
          echo "Template size: $(wc -c < "$TEMPLATE") bytes"

      - name: 3 - Select existing AWS role
        id: role
        shell: bash
        env:
          ROLE_ALIAS: ${{ inputs.roleAlias }}
          NETWORK: ${{ secrets.NETWORK_SERVICES_ROLE }}
          DIRECTORY: ${{ secrets.DIRECTORY_SERVICES_ROLE }}
          SECURITY: ${{ secrets.SECURITY_INFRA_ROLE }}
          SHAREDSERVICES: ${{ secrets.SHAREDSERVICES_ROLE }}
          MANAGEMENT: ${{ secrets.MGMT_ROLE }}
          LOGARCHIVE: ${{ secrets.LOGARCHIVE_ROLE }}
          COREAUDIT: ${{ secrets.COREAUDIT_ROLE }}
          MONITORING: ${{ secrets.MONITORING_ROLE }}
          DIRECTORY_DEV: ${{ secrets.DIRECTORY_SERVICES_DEV_ROLE }}
          DIRECTORY_PPE: ${{ secrets.DIRECTORY_SERVICES_PPE_ROLE }}
          MONITORING_PPE: ${{ secrets.PPE_MONITORING_SERVICE_ROLE }}
          SHAREDSERVICES_DEV: ${{ secrets.DEV_SHAREDSERVICES_ROLE }}
          SHAREDSERVICES_PPE: ${{ secrets.PPE_SHARED_SERVICE_ROLE }}
          SANDBOX: ${{ secrets.SANDBOX_ROLE }}
          TST_NETWORK: ${{ secrets.TST_NETWORK_SERVICE_ROLE }}
          PPE_NETWORK: ${{ secrets.PPE_NETWORK_SERVICE_ROLE }}
          BACKUP: ${{ secrets.BACKUP_ROLE }}
          CRV: ${{ secrets.CRV_ROLE }}
        run: |
          set -euo pipefail

          case "$ROLE_ALIAS" in
            NETWORK) ARN="$NETWORK" ;;
            DIRECTORY) ARN="$DIRECTORY" ;;
            SECURITY) ARN="$SECURITY" ;;
            SHAREDSERVICES) ARN="$SHAREDSERVICES" ;;
            MANAGEMENT) ARN="$MANAGEMENT" ;;
            LOGARCHIVE) ARN="$LOGARCHIVE" ;;
            COREAUDIT) ARN="$COREAUDIT" ;;
            MONITORING) ARN="$MONITORING" ;;
            DIRECTORY-DEV) ARN="$DIRECTORY_DEV" ;;
            DIRECTORY-PPE) ARN="$DIRECTORY_PPE" ;;
            MONITORING-PPE) ARN="$MONITORING_PPE" ;;
            SHAREDSERVICES-DEV) ARN="$SHAREDSERVICES_DEV" ;;
            SHAREDSERVICES-PPE) ARN="$SHAREDSERVICES_PPE" ;;
            SANDBOX) ARN="$SANDBOX" ;;
            TST-NETWORK) ARN="$TST_NETWORK" ;;
            PPE-NETWORK) ARN="$PPE_NETWORK" ;;
            BACKUP) ARN="$BACKUP" ;;
            CRV) ARN="$CRV" ;;
            *) echo "ERROR: Unsupported role alias"; exit 1 ;;
          esac

          if [[ -z "$ARN" ]]; then
            echo "ERROR: No existing secret configured for $ROLE_ALIAS"
            exit 1
          fi

          echo "Using role alias: $ROLE_ALIAS"
          echo "arn=$ARN" >> "$GITHUB_OUTPUT"

      - name: 4 - Assume AWS role
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ steps.role.outputs.arn }}
          role-session-name: cfn-check-${{ github.run_id }}
          aws-region: ${{ inputs.AWS_REGION }}

      - name: 5 - Confirm AWS account
        run: |
          set -euo pipefail
          aws sts get-caller-identity
          echo "AWS credentials configured successfully."

      - name: 6 - Check whether stack exists
        id: stack
        shell: bash
        env:
          STACK_NAME: ${{ inputs.stackName }}
        run: |
          set -euo pipefail

          if aws cloudformation describe-stacks \
              --stack-name "$STACK_NAME" \
              >/tmp/stack.json 2>/tmp/stack-error.txt; then

            echo "exists=true" >> "$GITHUB_OUTPUT"
            echo "Stack exists. Checking its status..."

            aws cloudformation describe-stacks \
              --stack-name "$STACK_NAME" \
              --query 'Stacks[0].StackStatus' \
              --output text

          elif grep -qi "does not exist" /tmp/stack-error.txt; then

            echo "exists=false" >> "$GITHUB_OUTPUT"
            echo "Fresh stack: $STACK_NAME does not exist."

          else
            cat /tmp/stack-error.txt
            echo "ERROR: Could not determine stack existence."
            exit 1
          fi

      - name: 7 - Detect drift on existing stack
        id: drift
        if: steps.stack.outputs.exists == 'true'
        shell: bash
        env:
          STACK_NAME: ${{ inputs.stackName }}
        run: |
          set -euo pipefail

          echo "Starting drift detection for $STACK_NAME"

          DETECTION_ID=$(aws cloudformation detect-stack-drift \
            --stack-name "$STACK_NAME" \
            --query StackDriftDetectionId \
            --output text)

          echo "Drift detection ID: $DETECTION_ID"
          echo "Waiting for drift detection to finish..."

          aws cloudformation wait stack-drift-detection-complete \
            --stack-drift-detection-id "$DETECTION_ID"

          STATUS=$(aws cloudformation describe-stack-drift-detection-status \
            --stack-drift-detection-id "$DETECTION_ID" \
            --query StackDriftStatus \
            --output text)

          echo "Drift status: $STATUS"

          if [[ "$STATUS" != "IN_SYNC" ]]; then
            echo "STOP: Stack is not IN_SYNC."
            echo "Resource-level drift details:"

            aws cloudformation describe-stack-resource-drifts \
              --stack-name "$STACK_NAME" \
              --query "StackResourceDrifts[?StackResourceDriftStatus=='MODIFIED' || StackResourceDriftStatus=='DELETED' || StackResourceDriftStatus=='NOT_CHECKED'].[LogicalResourceId,ResourceType,StackResourceDriftStatus,PhysicalResourceId]" \
              --output table || true

            echo "No change set will be created."
            exit 1
          fi

          echo "Stack is IN_SYNC. Proceeding to change-set creation."

      - name: 8 - Create change set (never deploy)
        id: changeset
        shell: bash
        env:
          STACK_NAME: ${{ inputs.stackName }}
          REGION: ${{ inputs.AWS_REGION }}
          STACK_EXISTS: ${{ steps.stack.outputs.exists }}
        run: |
          set -euo pipefail

          if [[ "$STACK_EXISTS" == "true" ]]; then
            TYPE="UPDATE"
          else
            TYPE="CREATE"
          fi

          echo "Change set type: $TYPE"
          echo "Stack: $STACK_NAME"
          echo "Region: $REGION"

          # CloudFormation TemplateBody has a 51,200-byte limit.
          SIZE=$(wc -c < "$TEMPLATE")
          if (( SIZE > 51200 )); then
            echo "ERROR: Template exceeds 51,200 bytes."
            echo "Use the existing S3 template-upload approach for large templates."
            exit 1
          fi

          CHANGE_SET_NAME="github-${GITHUB_RUN_ID}"

          if [[ "$TYPE" == "CREATE" ]]; then
            aws cloudformation create-change-set \
              --stack-name "$STACK_NAME" \
              --change-set-name "$CHANGE_SET_NAME" \
              --change-set-type CREATE \
              --template-body "file://$TEMPLATE" \
              --parameters "file://$PARAMS" \
              --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
              --region "$REGION"
          else
            aws cloudformation create-change-set \
              --stack-name "$STACK_NAME" \
              --change-set-name "$CHANGE_SET_NAME" \
              --change-set-type UPDATE \
              --template-body "file://$TEMPLATE" \
              --parameters "file://$PARAMS" \
              --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
              --region "$REGION"
          fi

          echo "Waiting for CloudFormation to process the change set..."

          for i in $(seq 1 60); do
            STATUS=$(aws cloudformation describe-change-set \
              --stack-name "$STACK_NAME" \
              --change-set-name "$CHANGE_SET_NAME" \
              --query Status \
              --output text \
              --region "$REGION")

            REASON=$(aws cloudformation describe-change-set \
              --stack-name "$STACK_NAME" \
              --change-set-name "$CHANGE_SET_NAME" \
              --query StatusReason \
              --output text \
              --region "$REGION")

            echo "Change set status: $STATUS"

            if [[ "$STATUS" == "CREATE_COMPLETE" ||
                  "$STATUS" == "FAILED" ]]; then
              break
            fi

            sleep 5
          done

          echo "Final change-set status: $STATUS"
          echo "Reason: $REASON"

          aws cloudformation describe-change-set \
            --stack-name "$STACK_NAME" \
            --change-set-name "$CHANGE_SET_NAME" \
            --region "$REGION" \
            --query '{StackId:StackId,ChangeSetId:ChangeSetId,ChangeSetName:ChangeSetName,Status:Status,ExecutionStatus:ExecutionStatus,StatusReason:StatusReason,Changes:Changes[*].ResourceChange.{Action:Action,LogicalResourceId:LogicalResourceId,ResourceType:ResourceType,Replacement:Replacement}}' \
            --output json

          echo "Change-set review is complete."
          echo "STOP: This workflow will NOT execute the change set."
```

### What you'll see in the logs

| Situation                       | Result                                                  |
| ------------------------------- | ------------------------------------------------------- |
| New stack                       | Fresh stack... → CREATE change set                      |
| Existing stack, no drift        | Stack is IN_SYNC → UPDATE change set                    |
| Existing stack, drift found     | Prints drift details → stops before change-set creation |
| Change set created              | Prints status, reason, and resource changes → stops     |
| AWS permissions or other errors | Workflow fails with the error                           |

### Three important limitations

* No deployment: There is no `execute-change-set` command in this YAML.

* Large templates: This minimal version supports templates up to 51,200 bytes. Larger templates need the S3 upload mechanism from your existing workflow.

* Permissions: Your existing role must permit drift-detection APIs and `cloudformation:CreateChangeSet`, plus the resource read permissions needed by drift detection.

One more thing: if `params.json` uses `UsePreviousValue` entries instead of `ParameterValue`, the parameter validation and AWS CLI handling will need a small adjustment. This version assumes the same `ParameterKey` / `ParameterValue` format used by the earlier workflow.
