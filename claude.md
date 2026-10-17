# ─────────────────────────────────────────────────────────────────────────────
#   🧰 Prepare  →  🔎 Drift check  →  📝 Change set      (you execute it in the AWS console)
#
#   Every stage is its own job, so the run page draws them as connected boxes
#   that turn green (passed), red (failed) or grey (skipped).
#     • Drift check runs only for an existing stack – any drift stops the run.
#     • The pipeline never deploys. It stops once the change set is ready, prints
#       exactly what will change, and links straight to it in the AWS console.
#   Inputs, secrets and BUCKET_NAME are exactly the same as before.
# ─────────────────────────────────────────────────────────────────────────────
name: CloudFormation Stack using S3 as required
run-name: "📝 ${{ inputs.stackName }} · ${{ inputs.roleAlias }} · ${{ inputs.AWS_REGION == 'other' && inputs.CUSTOM_REGION || inputs.AWS_REGION }}"

on:
  workflow_dispatch:
    inputs:
      folderPath:
        description: "Path under cloudformation/ (e.g. 'vpc' or 'network/peering')"
        required: true
      stackName:
        description: "Name of CloudFormation stack to deploy"
        required: true
      roleAlias:
        description: "Alias for the IAM role to assume"
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
        description: "AWS Region for deployment – pick one, or 'other' and type it under Custom region"
        required: false
        type: choice
        default: 'eu-west-1'
        options:
          - eu-west-1
          - us-east-1
          - other
      CUSTOM_REGION:
        description: "Only used when AWS Region is 'other' (e.g. ap-south-1)"
        required: false
        default: ''

permissions:
  id-token: write
  contents: read

# One run per stack at a time: a second run on the same stack waits instead of colliding.
concurrency:
  group: cfn-${{ inputs.roleAlias }}-${{ inputs.AWS_REGION == 'other' && inputs.CUSTOM_REGION || inputs.AWS_REGION }}-${{ inputs.stackName }}
  cancel-in-progress: false

env:
  BUCKET_NAME: ss-cfts3
  # The workflow inputs, handed to the scripts as plain env vars (nothing new to configure).
  STACK_NAME: ${{ inputs.stackName }}
  FOLDER_PATH: ${{ inputs.folderPath }}
  ROLE_ALIAS: ${{ inputs.roleAlias }}
  # Region = the dropdown pick, or the Custom region text when the pick is 'other'
  REGION: ${{ inputs.AWS_REGION == 'other' && inputs.CUSTOM_REGION || inputs.AWS_REGION }}

defaults:
  run:
    shell: bash   # runs as bash -eo pipefail: any failing command fails the stage

jobs:
  # ── 1 · Prepare: resolve the role, find the files, check the stack ─────────────
  prepare:
    name: 🧰 Prepare
    runs-on: ubuntu-latest
    outputs:
      role_secret: ${{ steps.setrole.outputs.secret }}
      template_file: ${{ steps.find_files.outputs.template_file }}
      params_file: ${{ steps.find_files.outputs.params_file }}
      large: ${{ steps.find_files.outputs.large }}
      mode: ${{ steps.stack.outputs.mode }}
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set role from alias
        id: setrole
        run: |
          region_re='^[a-z]{2}(-[a-z]+)+-[0-9]+$'
          if [[ ! "$REGION" =~ $region_re ]]; then
            if [ "$REGION" = other ]; then
              msg="Choose a region, or type one under 'Custom region' (e.g. ap-south-1)."
            else
              msg="'$REGION' is not a valid AWS region (e.g. eu-west-1)."
            fi
            echo "::error title=Invalid region::$msg"
            exit 1
          fi

          case "$ROLE_ALIAS" in
            NETWORK)            secret=NETWORK_SERVICES_ROLE ;;
            DIRECTORY)          secret=DIRECTORY_SERVICES_ROLE ;;
            SECURITY)           secret=SECURITY_INFRA_ROLE ;;
            SHAREDSERVICES)     secret=SHAREDSERVICES_ROLE ;;
            MANAGEMENT)         secret=MGMT_ROLE ;;
            LOGARCHIVE)         secret=LOGARCHIVE_ROLE ;;
            COREAUDIT)          secret=COREAUDIT_ROLE ;;
            MONITORING)         secret=MONITORING_ROLE ;;
            DIRECTORY-DEV)      secret=DIRECTORY_SERVICES_DEV_ROLE ;;
            DIRECTORY-PPE)      secret=DIRECTORY_SERVICES_PPE_ROLE ;;
            MONITORING-PPE)     secret=PPE_MONITORING_SERVICE_ROLE ;;
            SHAREDSERVICES-DEV) secret=DEV_SHAREDSERVICES_ROLE ;;
            SHAREDSERVICES-PPE) secret=PPE_SHARED_SERVICE_ROLE ;;
            SANDBOX)            secret=SANDBOX_ROLE ;;
            TST-NETWORK)        secret=TST_NETWORK_SERVICE_ROLE ;;
            PPE-NETWORK)        secret=PPE_NETWORK_SERVICE_ROLE ;;
            BACKUP)             secret=BACKUP_ROLE ;;
            CRV)                secret=CRV_ROLE ;;
            # Add more mappings here
            *)
              echo "::error title=Unknown role alias::No secret found for alias '$ROLE_ALIAS'"
              exit 1
              ;;
          esac
          echo "🔑 Role alias : $ROLE_ALIAS → secret $secret"
          echo "secret=$secret" >> "$GITHUB_OUTPUT"

      - name: Find template and params
        id: find_files
        env:
          ROLE_ARN: ${{ secrets[steps.setrole.outputs.secret] }}
        run: |
          fail() { echo "::error title=Prepare failed::$1"; exit 1; }

          [ -n "$ROLE_ARN" ] || fail "The secret for role alias '$ROLE_ALIAS' is empty or not set."
          [[ "$STACK_NAME" =~ ^[A-Za-z][A-Za-z0-9-]{0,127}$ ]] ||
            fail "'$STACK_NAME' is not a valid stack name (letters, digits and hyphens, starting with a letter)."

          dir="cloudformation/${FOLDER_PATH}"
          [ -d "$dir" ] || fail "Folder $dir not found."

          # Template directly in the folder first; sub-folders only as a fallback (as before).
          match=( -type f \( -iname "*-template.yaml" -o -iname "*-template.yml" \) )
          mapfile -t found < <(find "$dir" -maxdepth 1 "${match[@]}" | sort)
          [ "${#found[@]}" -gt 0 ] || mapfile -t found < <(find "$dir" "${match[@]}" | sort)
          [ "${#found[@]}" -gt 0 ] || fail "No template matching '*-template.y[a]ml' found in $dir"
          if [ "${#found[@]}" -gt 1 ]; then
            echo "::warning title=More than one template::${#found[@]} templates in $dir – using ${found[0]}"
          fi
          template_file="${found[0]}"

          params_file="$dir/params.json"
          [ -f "$params_file" ] || fail "params.json not found in $dir"
          jq -e 'type == "array"' "$params_file" > /dev/null ||
            fail "$params_file must be a JSON list of ParameterKey/ParameterValue objects."

          size=$(stat --format=%s "$template_file")
          large=false
          if [ "$size" -gt 51200 ]; then large=true; fi

          echo "📄 Template   : $template_file ($size bytes)"
          echo "📄 Parameters : $params_file ($(jq length "$params_file") entries)"
          if [ "$large" = true ]; then echo "📦 Over 51,200 bytes – the template will go through S3."; fi
          {
            echo "template_file=$template_file"
            echo "params_file=$params_file"
            echo "size=$size"
            echo "large=$large"
          } >> "$GITHUB_OUTPUT"

      - name: Configure AWS credentials for deployment
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets[steps.setrole.outputs.secret] }}
          role-session-name: cft_deployment
          aws-region: ${{ env.REGION }}

      - name: Check stack status
        id: stack
        env:
          TEMPLATE_FILE: ${{ steps.find_files.outputs.template_file }}
          PARAMS_FILE: ${{ steps.find_files.outputs.params_file }}
          SIZE: ${{ steps.find_files.outputs.size }}
          LARGE: ${{ steps.find_files.outputs.large }}
        run: |
          account=$(aws sts get-caller-identity --query Account --output text)
          echo "☁️  Account    : $account ($REGION)"

          if ! status=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
                          --query 'Stacks[0].StackStatus' --output text 2> "$RUNNER_TEMP/err"); then
            grep -q 'does not exist' "$RUNNER_TEMP/err" || { cat "$RUNNER_TEMP/err"; exit 1; }
            status=NOT_FOUND
          fi

          case "$status" in
            NOT_FOUND|REVIEW_IN_PROGRESS)
              mode=create
              plan="🆕 New stack → CREATE change set (drift check not needed)" ;;
            CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE|IMPORT_COMPLETE|IMPORT_ROLLBACK_COMPLETE)
              mode=update
              plan="🔄 Exists ($status) → drift check, then UPDATE change set" ;;
            ROLLBACK_COMPLETE)
              echo "::error title=Stack can't be updated::$STACK_NAME is in ROLLBACK_COMPLETE (its first create failed). Delete the stack in the console, then re-run."
              exit 1 ;;
            *_IN_PROGRESS)
              echo "::error title=Stack is busy::$STACK_NAME is $status. Wait for it to finish, then re-run."
              exit 1 ;;
            *)
              echo "::error title=Stack can't be updated::$STACK_NAME is in $status. Fix it in the CloudFormation console, then re-run."
              exit 1 ;;
          esac
          echo "🏗️  Stack      : $STACK_NAME – $plan"
          echo "mode=$mode" >> "$GITHUB_OUTPUT"

          how="sent inline"
          if [ "$LARGE" = true ]; then how="uploaded to S3 – over 51,200 bytes"; fi
          {
            echo "### 🧰 Prepare"
            echo "| | |"
            echo "|---|---|"
            printf '| **Stack** | `%s` |\n' "$STACK_NAME"
            printf '| **Account** | %s · `%s` · %s |\n' "$ROLE_ALIAS" "$account" "$REGION"
            printf '| **Template** | `%s` (%s bytes, %s) |\n' "$TEMPLATE_FILE" "$SIZE" "$how"
            printf '| **Parameters** | `%s` |\n' "$PARAMS_FILE"
            printf '| **Plan** | %s |\n' "$plan"
          } >> "$GITHUB_STEP_SUMMARY"

  # ── 2 · Drift check: existing stacks only – stop if anything drifted ──────────
  drift:
    name: 🔎 Drift check
    needs: prepare
    if: needs.prepare.outputs.mode == 'update'
    runs-on: ubuntu-latest
    steps:
      - name: Configure AWS credentials for deployment
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets[needs.prepare.outputs.role_secret] }}
          role-session-name: cft_deployment
          aws-region: ${{ env.REGION }}

      - name: Detect drift
        run: |
          echo "🔎 Checking $STACK_NAME for drift (changes made outside CloudFormation)…"
          id=$(aws cloudformation detect-stack-drift --stack-name "$STACK_NAME" \
                 --query StackDriftDetectionId --output text)
          while :; do
            res=$(aws cloudformation describe-stack-drift-detection-status \
                    --stack-drift-detection-id "$id" --output json)
            [ "$(jq -r .DetectionStatus <<< "$res")" = DETECTION_IN_PROGRESS ] || break
            echo "   … still checking"
            sleep 10
          done
          detection=$(jq -r .DetectionStatus <<< "$res")
          drift=$(jq -r '.StackDriftStatus // "UNKNOWN"' <<< "$res")
          count=$(jq -r '.DriftedStackResourceCount // 0' <<< "$res")
          reason=$(jq -r '.DetectionStatusReason // ""' <<< "$res")

          if [ "$drift" = DRIFTED ]; then
            aws cloudformation describe-stack-resource-drifts --stack-name "$STACK_NAME" \
              --stack-resource-drift-status-filters MODIFIED DELETED --output json > "$RUNNER_TEMP/drift.json"

            # Drifted resources as a table, then the property-level differences
            jq -r '
              def pad($n): tostring | if length >= $n then . else . + (" " * ($n - length)) end;
              "   \("DRIFT" | pad(10))\("RESOURCE" | pad(40))\("TYPE" | pad(42))PHYSICAL ID",
              (.StackResourceDrifts[]
               | "   \(.StackResourceDriftStatus | pad(10))\(.LogicalResourceId | pad(40))\(.ResourceType | pad(42))\(.PhysicalResourceId // "-")")
            ' "$RUNNER_TEMP/drift.json" > "$RUNNER_TEMP/drift_table.txt"
            jq -r '
              .StackResourceDrifts[] | select(.StackResourceDriftStatus == "MODIFIED")
              | .LogicalResourceId as $id | .PropertyDifferences[]?
              | "• \($id) → \(.PropertyPath) (\(.DifferenceType))\n    expected: \(.ExpectedValue)\n    actual  : \(.ActualValue)"
            ' "$RUNNER_TEMP/drift.json" > "$RUNNER_TEMP/drift_diff.txt"

            echo
            echo "❌ DRIFT FOUND – $count resource(s) no longer match the template:"
            echo
            cat "$RUNNER_TEMP/drift_table.txt"
            if [ -s "$RUNNER_TEMP/drift_diff.txt" ]; then
              echo
              echo "What changed (expected = template, actual = live in AWS):"
              cat "$RUNNER_TEMP/drift_diff.txt"
            fi
            echo
            echo "⛔ Stopping before the change set. Undo the manual change (or add it to the template), then re-run."

            {
              echo "### 🔎 Drift check"
              echo "❌ **Drift found in $count resource(s) – run stopped before the change set.**"
              echo
              echo "| Drift | Resource | Type | Physical ID |"
              echo "|---|---|---|---|"
              jq -r '.StackResourceDrifts[] | "| \(.StackResourceDriftStatus) | `\(.LogicalResourceId)` | \(.ResourceType) | \(.PhysicalResourceId // "-") |"' "$RUNNER_TEMP/drift.json"
              if [ -s "$RUNNER_TEMP/drift_diff.txt" ]; then
                echo
                echo '```text'
                cat "$RUNNER_TEMP/drift_diff.txt"
                echo '```'
              fi
            } >> "$GITHUB_STEP_SUMMARY"
            echo "::error title=Drift detected::$count resource(s) in $STACK_NAME drifted from the template – stopped before the change set."
            exit 1
          fi

          if [ "$drift" = IN_SYNC ] && [ "$detection" = DETECTION_COMPLETE ]; then
            echo "✅ No drift – every resource matches the template."
            printf '### 🔎 Drift check\n✅ No drift – the stack matches its template.\n' >> "$GITHUB_STEP_SUMMARY"
          else
            msg="Drift check couldn't cover every resource ($detection, $drift)${reason:+: $reason}"
            echo "⚠️  $msg – continuing."
            echo "::warning title=Drift check incomplete::$msg"
            printf '### 🔎 Drift check\n⚠️ %s – continuing.\n' "$msg" >> "$GITHUB_STEP_SUMMARY"
          fi

  # ── 3 · Change set: create it, show what will change, then STOP ───────────────
  changeset:
    name: 📝 Change set
    needs: [prepare, drift]
    if: ${{ !cancelled() && needs.prepare.result == 'success' && (needs.drift.result == 'success' || needs.drift.result == 'skipped') }}
    runs-on: ubuntu-latest
    env:
      TEMPLATE_FILE: ${{ needs.prepare.outputs.template_file }}
      PARAMS_FILE: ${{ needs.prepare.outputs.params_file }}
      MODE: ${{ needs.prepare.outputs.mode }}
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Configure AWS credentials for S3 upload
        if: needs.prepare.outputs.large == 'true'
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.S3_ACCESS_GITHUB_ROLE }}
          role-session-name: s3_upload
          aws-region: ${{ env.REGION }}

      - name: Generate pre-signed PUT/GET URLs and upload template
        if: needs.prepare.outputs.large == 'true'
        id: presign_upload
        run: |
          key="${STACK_NAME}-$(date +%s).yaml"   # unique key per run
          echo "📦 Uploading the large template to S3 as $key"
          pip install --quiet boto3
          urls=$(python cloudformation/production/shared_services/Github_s3/presign.py "$key")
          put_url=$(grep '^put_url=' <<< "$urls" | cut -d'=' -f2- || true)
          get_url=$(grep '^get_url=' <<< "$urls" | cut -d'=' -f2- || true)
          if [ -z "$put_url" ] || [ -z "$get_url" ]; then
            echo "::error title=Upload failed::presign.py did not return put_url and get_url"
            exit 1
          fi
          curl --fail --silent --show-error -X PUT -T "$TEMPLATE_FILE" "$put_url" ||
            { echo "::error title=Upload failed::Could not upload the template to S3"; exit 1; }
          echo "✔ Uploaded"
          echo "get_url=$get_url" >> "$GITHUB_OUTPUT"

      - name: Configure AWS credentials for deployment
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets[needs.prepare.outputs.role_secret] }}
          role-session-name: cft_deployment
          aws-region: ${{ env.REGION }}

      - name: Create change set
        env:
          TEMPLATE_URL: ${{ steps.presign_upload.outputs.get_url }}
        run: |
          if [ -n "$TEMPLATE_URL" ]; then
            tpl=(--template-url "$TEMPLATE_URL")
          else
            tpl=(--template-body "file://$TEMPLATE_FILE")
          fi

          echo "🧪 Checking template…"
          aws cloudformation get-template-summary "${tpl[@]}" --output json > "$RUNNER_TEMP/template.json"
          echo "✔ Template OK – $(jq '.Parameters // [] | length' "$RUNNER_TEMP/template.json") parameter(s) declared"

          # Parameters follow the same rules as `aws cloudformation deploy`:
          #   • value from params.json when it's there
          #   • not in params.json → existing stack keeps its current value, new stack uses the template default
          #   • keys in params.json that the template doesn't declare are ignored (with a warning)
          if [ "$MODE" = update ]; then
            aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
              --query 'Stacks[0].Parameters[].ParameterKey' --output json > "$RUNNER_TEMP/current.json"
          else
            echo '[]' > "$RUNNER_TEMP/current.json"
          fi
          jq --slurpfile tpl "$RUNNER_TEMP/template.json" --slurpfile cur "$RUNNER_TEMP/current.json" '
            . as $given
            | ($cur[0] // []) as $current
            | [ ($tpl[0].Parameters // [])[].ParameterKey as $k
                | ([$given[] | select(.ParameterKey == $k)][0]) as $p
                | if $p != null and ($p | has("ParameterValue")) then
                    {ParameterKey: $k, ParameterValue: ($p.ParameterValue | if type == "array" then join(",") else tostring end)}
                  elif ($current | index($k)) != null then
                    {ParameterKey: $k, UsePreviousValue: true}
                  else empty end ]
          ' "$PARAMS_FILE" > "$RUNNER_TEMP/params.json"

          # NoEcho parameter values are masked everywhere in this log
          jq -r --slurpfile tpl "$RUNNER_TEMP/template.json" '
            [($tpl[0].Parameters // [])[] | select(.NoEcho) | .ParameterKey] as $hidden
            | .[] | select(.ParameterKey as $k | $hidden | index($k) != null)
            | .ParameterValue // empty | select(length > 0)
          ' "$RUNNER_TEMP/params.json" | while IFS= read -r value; do echo "::add-mask::$value"; done

          jq -r --slurpfile tpl "$RUNNER_TEMP/template.json" '
            (($tpl[0].Parameters // []) | map(.ParameterKey)) as $declared
            | .[].ParameterKey | select(. as $k | $declared | index($k) == null)
          ' "$PARAMS_FILE" | while read -r key; do
            echo "::warning title=Unused parameter::'$key' is in params.json but not in the template – ignored"
          done

          echo "::group::📋 Parameters ($(jq length "$RUNNER_TEMP/params.json"))"
          jq -r --slurpfile tpl "$RUNNER_TEMP/template.json" '
            [($tpl[0].Parameters // [])[] | select(.NoEcho) | .ParameterKey] as $hidden
            | .[] | "   \(.ParameterKey) = " + (
                if .UsePreviousValue then "(keeps current value)"
                elif (.ParameterKey as $k | $hidden | index($k)) != null then "****"
                else .ParameterValue end)
          ' "$RUNNER_TEMP/params.json"
          echo "::endgroup::"

          params=()
          if [ "$(jq length "$RUNNER_TEMP/params.json")" -gt 0 ]; then
            params=(--parameters "file://$RUNNER_TEMP/params.json")
          fi

          cs_name="gha-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
          cs_type=UPDATE
          if [ "$MODE" = create ]; then cs_type=CREATE; fi
          echo "📝 Creating $cs_type change set $cs_name…"
          if ! created=$(aws cloudformation create-change-set \
                           --stack-name "$STACK_NAME" \
                           --change-set-name "$cs_name" \
                           --change-set-type "$cs_type" \
                           "${tpl[@]}" "${params[@]}" \
                           --capabilities CAPABILITY_NAMED_IAM \
                           --description "GitHub Actions run ${GITHUB_RUN_ID} by ${GITHUB_ACTOR}" \
                           --output json); then
            echo "::error title=Change set failed::CloudFormation rejected the change set – see the error above."
            exit 1
          fi
          cs_id=$(jq -r .Id <<< "$created")
          stack_id=$(jq -r .StackId <<< "$created")

          while :; do
            cs=$(aws cloudformation describe-change-set --stack-name "$STACK_NAME" \
                   --change-set-name "$cs_name" --include-property-values --output json)
            status=$(jq -r .Status <<< "$cs")
            case "$status" in
              CREATE_PENDING|CREATE_IN_PROGRESS) sleep 5 ;;
              *) break ;;
            esac
          done
          reason=$(jq -r '.StatusReason // ""' <<< "$cs")

          # Nothing to change → this stage passes and there is nothing to execute
          if [ "$status" = FAILED ] && grep -qE "didn't contain changes|No updates are to be performed" <<< "$reason"; then
            echo "✅ No changes – the stack already matches this template and these parameters. Nothing to execute."
            aws cloudformation delete-change-set --stack-name "$STACK_NAME" --change-set-name "$cs_name" || true
            printf '### 📝 Change set\n✅ No changes – the stack is already up to date. Nothing to execute.\n' >> "$GITHUB_STEP_SUMMARY"
            exit 0
          fi

          if [ "$status" != CREATE_COMPLETE ]; then
            echo "❌ Change set $status: $reason"
            echo "::error title=Change set failed::$reason"
            printf '### 📝 Change set\n❌ **%s** – %s\n' "$status" "$reason" >> "$GITHUB_STEP_SUMMARY"
            exit 1
          fi

          # ── What will change: one line per resource, then each property with before → after ──
          read -r n_all n_add n_mod n_del n_rep <<< "$(jq -r '
            [.Changes[]?.ResourceChange // empty]
            | [., map(select(.Action == "Add")), map(select(.Action == "Modify")),
               map(select(.Action == "Remove")), map(select(.Replacement == "True"))]
            | map(length) | @tsv' <<< "$cs")"

          jq -r '
            def pad($n): tostring | if length >= $n then . else . + (" " * ($n - length)) end;
            def clip: if length > 300 then .[:300] + " …" else . end;
            # JSON values print as-is, plain text gets quotes
            def fmt: if . == null then "(none)" else tostring | . as $s
                     | (try ($s | fromjson | type) catch "string") as $t
                     | (if $t == "string" then ($s | tojson) else $s end) | clip end;
            def secretish: test("password|secret|token|passphrase|credential|privatekey"; "i");
            def icon: if .Action == "Add" then "➕" elif .Action == "Remove" then "➖"
                      elif .Replacement == "True" then "🔄" elif .Action == "Modify" then "🔧" else "• " end;
            [.Changes[]?.ResourceChange // empty] as $changes
            | if ($changes | length) == 0 then
                "   (no resource changes – stack-level update only, e.g. outputs or tags)"
              else
                $changes[]
                | (("   \(icon) \(.Action | pad(8)) \(.LogicalResourceId | pad(38)) \(.ResourceType | pad(42)) "
                    + (if .Replacement == "True" then "⚠ REPLACEMENT"
                       elif .Replacement == "Conditional" then "may be replaced"
                       elif .Action == "Remove" then "(\(.PolicyAction // "Delete"))"
                       else "" end)) | sub(" +$"; "")),
                  ([.Details[]?.Target // empty
                    | {path: (.Path // ("/" + .Attribute + (if .Name then "/" + .Name else "" end))),
                       type: (.AttributeChangeType // ""), before: .BeforeValue, after: .AfterValue,
                       rec: (.RequiresRecreation // "Never")}]
                   | unique_by([.path, .type, .before, .after])[]
                   | (.path | secretish) as $hide
                   | (if $hide and .before != null then "(hidden)" else (.before | fmt) end) as $b
                   | (if $hide and .after != null then "(hidden)" else (.after | fmt) end) as $a
                   | "        \(if .type == "Add" then "+" elif .type == "Remove" then "-" elif .type == "Modify" then "~" else "•" end) \(.path | pad(42)) "
                     + (if .type == "Add" then "→ \($a)"
                        elif .type == "Remove" then "\($b) → (removed)"
                        elif .before == null and .after == null then "(resolved at deploy time)"
                        else "\($b) → \($a)" end)
                     + (if .rec == "Always" then "   ⚠ forces replacement"
                        elif .rec == "Conditionally" then "   (may force replacement)" else "" end))
              end
          ' <<< "$cs" > "$RUNNER_TEMP/changes.txt"

          echo "📋 Planned changes: $n_add to add · $n_mod to modify · $n_del to remove"
          echo
          cat "$RUNNER_TEMP/changes.txt"
          echo
          if [ "$n_rep" -gt 0 ]; then
            echo "::warning title=Replacement::$n_rep resource(s) will be REPLACED (deleted and re-created) if you execute this change set"
          fi
          if [ "$n_del" -gt 0 ]; then
            echo "::warning title=Removal::$n_del resource(s) will be removed if you execute this change set"
          fi

          # Older pipeline change sets still waiting on this stack could be executed by mistake
          others=$(aws cloudformation list-change-sets --stack-name "$STACK_NAME" --output json 2> /dev/null |
                     jq -r --arg me "$cs_name" '[.Summaries[]? | select(.ChangeSetName != $me and .ExecutionStatus == "AVAILABLE") | .ChangeSetName] | join(", ")' || true)
          if [ -n "$others" ]; then
            echo "::warning title=Older change sets waiting::$STACK_NAME also has older change sets ready: $others. Execute only $cs_name, or delete the old ones."
          fi

          # Direct link to this change set in the console
          account=$(cut -d: -f5 <<< "$stack_id")
          enc() { jq -rn --arg v "$1" '$v | @uri'; }
          console="https://${REGION}.console.aws.amazon.com/cloudformation/home?region=${REGION}#/stacks/changesets/changes?stackId=$(enc "$stack_id")&changeSetId=$(enc "$cs_id")"

          echo "✅ Change set $cs_name is ready. Nothing has been deployed."
          echo "👉 Review and execute it in the AWS console (account $account · $ROLE_ALIAS · $REGION):"
          echo "   $console"
          echo "::notice title=Change set ready – not deployed::Review and execute $cs_name in the AWS console (account $account, $REGION). The link is in the log and the run summary."

          {
            echo "### 📝 Change set ready for review – nothing deployed yet"
            printf '`%s` (%s) on `%s` · account `%s` (%s) · %s\n\n' "$cs_name" "$cs_type" "$STACK_NAME" "$account" "$ROLE_ALIAS" "$REGION"
            printf '**%s** to add · **%s** to modify · **%s** to remove' "$n_add" "$n_mod" "$n_del"
            if [ "$n_rep" -gt 0 ]; then printf ' · ⚠️ **%s** replacement(s)' "$n_rep"; fi
            printf '\n\n👉 **[Review and execute it in the AWS console](%s)**\n\n' "$console"
            if [ "$n_all" -gt 0 ]; then
              echo "| Action | Resource | Type | Replacement |"
              echo "|---|---|---|---|"
              jq -r '.Changes[]?.ResourceChange // empty | "| \(.Action) | `\(.LogicalResourceId)` | \(.ResourceType) | \(.Replacement // "-") |"' <<< "$cs"
              echo
              echo "<details><summary>Property-level changes (before → after)</summary>"
              echo
              echo '```text'
              cat "$RUNNER_TEMP/changes.txt"
              echo '```'
              echo "</details>"
            fi
          } >> "$GITHUB_STEP_SUMMARY"