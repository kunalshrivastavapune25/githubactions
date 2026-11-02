#!/usr/bin/env bash
# Deploys the account-level roles (iamr-<account>-<env>-deploy / -read) with one StackSet to the accounts listed in
# AccountRoles in platform-iam-roles-stackset-template.yml - the 14 Infrastructure and Security accounts that the
# setup test showed without roles. Only those accounts get a stack: the network accounts (also in the Infrastructure
# OU), Sandbox and the Workload accounts are not touched.
#
# Run it in IMS Root (390403857119) as an administrator, e.g. in AWS CloudShell, next to
# platform-iam-roles-stackset-template.yml. It needs the AWS CLI v2 and jq (CloudShell has both).
# The IMS Root roles must exist first (stacks ims-root-iam-roles-infrastructure and -security): the account roles'
# trust policies name them. The script checks this before it changes anything.
#
#   ./deploy-platform-roles-stackset.sh                          all accounts in AccountRoles
#   ONLY="697858908364" ./deploy-platform-roles-stackset.sh      a pilot: only these accounts (space-separated IDs)
#   EXCLUDE="816069122659" ./deploy-platform-roles-stackset.sh   all in AccountRoles except these
#
# Run it again after changing the template or to add accounts: it updates the StackSet and its stacks, then adds
# the accounts that are missing (for accounts that already have a stack, AWS just brings it up to date).
set -euo pipefail

STACK_SET="${STACK_SET:-ims-platform-iamr-roles}"
TEMPLATE="${TEMPLATE:-platform-iam-roles-stackset-template.yml}"
REGION="${REGION:-eu-west-1}"            # ONE Region only: IAM is global, a second Region would fail on the role names
OUS="${OUS:-Infrastructure Security}"    # OUs holding those accounts (child OUs such as Infrastructure > ppe included)
ONLY="${ONLY:-}"
EXCLUDE="${EXCLUDE:-}"
IMS_ROOT=390403857119
PREFS='{"RegionConcurrencyType":"SEQUENTIAL","MaxConcurrentPercentage":34,"FailureTolerancePercentage":100}'

fail() { echo "❌ $1" >&2; exit 1; }
[ -f "$TEMPLATE" ] || fail "Template $TEMPLATE not found (run this next to it, or set TEMPLATE=...)."
[ -z "$ONLY" ] || [ -z "$EXCLUDE" ] || fail "Use ONLY or EXCLUDE, not both."
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"   # the StackSet lives in the same Region as its stacks

account=$(aws sts get-caller-identity --query Account --output text)
[ "$account" = "$IMS_ROOT" ] || fail "Signed in to $account. Run this in IMS Root ($IMS_ROOT)."

# 0. The accounts to deploy to (from AccountRoles), and the IMS Root roles they will trust - those must exist first
mapfile -t map < <(sed -nE 's/^ *"([0-9]{12})": *\{Base: *([a-z0-9-]+)\}.*/\1 \2/p' "$TEMPLATE")
[ "${#map[@]}" -gt 0 ] || fail "No accounts found in AccountRoles in $TEMPLATE."
known=" ${map[*]} "
for id in $ONLY; do
  [[ "$known" == *" $id "* ]] || fail "Account $id is not in AccountRoles in $TEMPLATE."
done
targets_ids=()
missing=()
for line in "${map[@]}"; do
  read -r id base <<< "$line"
  if [ -n "$ONLY" ] && [[ " $ONLY " != *" $id "* ]]; then continue; fi
  if [ -n "$EXCLUDE" ] && [[ " $EXCLUDE " == *" $id "* ]]; then continue; fi
  targets_ids+=("$id")
  for access in deploy read; do
    aws iam get-role --role-name "$base-$access" > /dev/null 2>&1 || missing+=("$base-$access")
  done
done
[ "${#targets_ids[@]}" -gt 0 ] || fail "No accounts left to deploy to (check ONLY / EXCLUDE)."
if [ "${#missing[@]}" -gt 0 ]; then
  fail "Not in IMS Root yet: ${missing[*]}. Deploy the IMS Root stacks first (ims-root-iam-roles-infrastructure / -security)."
fi
echo "✔ ${#targets_ids[@]} account(s) to deploy to; the IMS Root roles they trust exist"

wait_for() {   # $1 = StackSet operation ID
  local status
  while :; do
    status=$(aws cloudformation describe-stack-set-operation --stack-set-name "$STACK_SET" --operation-id "$1" \
               --query StackSetOperation.Status --output text)
    case "$status" in
      RUNNING|QUEUED|STOPPING) echo "   … $status"; sleep 15 ;;
      *) echo "   → $status"; break ;;
    esac
  done
}

# 1. Trusted access between StackSets and AWS Organizations (once per organization)
access=$(aws cloudformation describe-organizations-access --query Status --output text)
if [ "$access" != ENABLED ]; then
  echo "🔓 Activating trusted access for StackSets with AWS Organizations (was $access)…"
  aws cloudformation activate-organizations-access
fi
echo "✔ Trusted access for StackSets: enabled"

# 2. The OUs that hold those accounts (a target OU includes its child OUs)
root_id=$(aws organizations list-roots --query 'Roots[0].Id' --output text)
all_ous=$(aws organizations list-organizational-units-for-parent --parent-id "$root_id" --output json)
ou_ids=()
for name in $OUS; do
  id=$(jq -r --arg n "$name" '.OrganizationalUnits[] | select(.Name == $n) | .Id' <<< "$all_ous")
  [ -n "$id" ] || fail "No OU named '$name' under the organization root."
  echo "✔ OU $name: $id"
  ou_ids+=("$id")
done

# 3. The StackSet: create it, or update it to this template
if aws cloudformation describe-stack-set --stack-set-name "$STACK_SET" > /dev/null 2>&1; then
  echo "🔄 Updating StackSet $STACK_SET to $TEMPLATE…"
  op=$(aws cloudformation update-stack-set --stack-set-name "$STACK_SET" --template-body "file://$TEMPLATE" \
         --capabilities CAPABILITY_NAMED_IAM --operation-preferences "$PREFS" --query OperationId --output text)
  wait_for "$op"
else
  echo "🆕 Creating StackSet $STACK_SET (service-managed, no automatic deployment)…"
  aws cloudformation create-stack-set --stack-set-name "$STACK_SET" --template-body "file://$TEMPLATE" \
    --permission-model SERVICE_MANAGED --auto-deployment Enabled=false \
    --capabilities CAPABILITY_NAMED_IAM \
    --description "GitHub pipeline roles (iamr-*-deploy/read) for the platform accounts that had none, assumed from IMS Root" > /dev/null
fi

# 4. Stack instances: ONLY the listed accounts (Intersection with the OUs), in one Region
targets=$(jq -cn --argjson ous "$(printf '%s\n' "${ou_ids[@]}" | jq -R . | jq -s .)" \
               --argjson ids "$(printf '%s\n' "${targets_ids[@]}" | jq -R . | jq -s .)" \
               '{OrganizationalUnitIds: $ous, Accounts: $ids, AccountFilterType: "INTERSECTION"}')
echo "🚀 Adding stack instances in $REGION for: ${targets_ids[*]}"
op=$(aws cloudformation create-stack-instances --stack-set-name "$STACK_SET" --deployment-targets "$targets" \
       --regions "$REGION" --operation-preferences "$PREFS" --query OperationId --output text)
wait_for "$op"

# 5. Result per account
instances=$(aws cloudformation list-stack-instances --stack-set-name "$STACK_SET" --output json)
echo
jq -r '.Summaries | sort_by(.Account)[]
  | "\(if .Status == "CURRENT" then "✅" else "❌" end) \(.Account)  \(.Region)  \(.Status)/\(.StackInstanceStatus.DetailedStatus // "-")  \(.StatusReason // "")"' <<< "$instances"
bad=$(jq '[.Summaries[] | select(.Status != "CURRENT")] | length' <<< "$instances")
total=$(jq '.Summaries | length' <<< "$instances")
echo
if [ "$bad" -gt 0 ]; then
  echo "❌ $bad of $total stack instance(s) are not CURRENT – see the reasons above."
  echo "   'already exists' = that account already has the roles (e.g. from a single stack). Either delete that single"
  echo "   stack and run this again, or keep it: remove the account from AccountRoles (or run with EXCLUDE=\"<account>\")"
  echo "   and delete its failed instance (console: StackSets → $STACK_SET → Actions → Delete stacks from StackSet →"
  echo "   that account, Retain stacks off; it created nothing)."
  exit 1
fi
echo "✅ All $total stack instance(s) are CURRENT. Next: run 'Test account setups' (OU Infrastructure, then Security)."
