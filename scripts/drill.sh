#!/usr/bin/env bash
# Run a restore drill: restore into a sandbox RDS instance, verify, delete it, write the report.
#   MODE=snapshot (default) | pitr      RTO_TARGET=45m   RPO_TARGET=24h
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need aws terraform python3
TF="$REPO_ROOT/infra/terraform"
ensure_restoredrill
tf_init "$TF" "restore-drill-kit/terraform.tfstate"
tf_has_resources "$TF" || die "Nothing is running. Start it first: bash scripts/up.sh"

report="$REPO_ROOT/reports/rds-$(date -u +%Y%m%dT%H%M%SZ)"
log "Restoring (${MODE:-snapshot}) into a sandbox instance - RDS needs ~10-20 minutes"
restoredrill rds-drill --mode "${MODE:-snapshot}" --with-isolation \
  --source-id "$(tf_out "$TF" source_id)" --sg-id "$(tf_out "$TF" sandbox_sg_id)" \
  --subnet-group "$(tf_out "$TF" subnet_group)" --manifest "$(tf_out "$TF" manifest_uri)" \
  --password-ssm "$(tf_out "$TF" password_ssm_name)" \
  --rto-target "${RTO_TARGET:-45m}" --rpo-target "${RPO_TARGET:-24h}" --report "$report"
log "Report: $report/report.html"
