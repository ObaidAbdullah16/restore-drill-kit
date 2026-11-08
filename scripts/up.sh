#!/usr/bin/env bash
# Start: create the RDS "production" database, load synthetic data, record a manifest
# and take a snapshot. Takes ~10-15 minutes (RDS creation). Cost while up: ~$0.04/hour.
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need aws terraform python3
TF="$REPO_ROOT/infra/terraform"
ensure_restoredrill

log "Terraform apply (VPC, RDS PostgreSQL 16, manifest bucket)"
tf_init "$TF" "restore-drill-kit/terraform.tfstate"
tf_apply "$TF"

pw="$(aws ssm get-parameter --name "$(tf_out "$TF" password_ssm_name)" --with-decryption --query Parameter.Value --output text)"
dsn="postgresql://$(tf_out "$TF" username):${pw}@$(tf_out "$TF" endpoint):$(tf_out "$TF" port)/$(tf_out "$TF" db_name)?sslmode=require"

log "Seeding synthetic data, saving the manifest and taking a snapshot"
restoredrill rds-prepare --seed --customers 2000 \
  --source-id "$(tf_out "$TF" source_id)" --dsn "$dsn" \
  --sg-id "$(tf_out "$TF" sg_id)" --manifest "$(tf_out "$TF" manifest_uri)"

log "Ready. Run a drill:  bash scripts/drill.sh     Stop everything:  bash scripts/down.sh"
