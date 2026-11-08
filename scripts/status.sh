#!/usr/bin/env bash
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need aws terraform
TF="$REPO_ROOT/infra/terraform"
tf_init "$TF" "restore-drill-kit/terraform.tfstate"
if tf_has_resources "$TF"; then
  echo "restore-drill-kit: UP (~\$0.04/hour)"
  aws rds describe-db-instances \
    --query "DBInstances[?starts_with(DBInstanceIdentifier, 'restore-drill')].[DBInstanceIdentifier,DBInstanceStatus,DBInstanceClass]" \
    --output table
else
  echo "restore-drill-kit: DOWN (costs nothing)"
fi
