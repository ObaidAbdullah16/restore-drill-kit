#!/usr/bin/env bash
# Stop: delete drill sandboxes and kit snapshots, then destroy everything Terraform made.
# Nothing is kept in AWS (there is nothing worth keeping), so `down --all` does the same.
# Your drill reports stay on your laptop in reports/.
#   bash scripts/down.sh      (or: bash scripts/down.sh --all)
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
for a in "$@"; do [[ "$a" == "--all" ]] || die "unknown option: $a (use --all or nothing)"; done
need aws terraform python3
TF="$REPO_ROOT/infra/terraform"
ensure_restoredrill
tf_init "$TF" "restore-drill-kit/terraform.tfstate"

log "Removing sandbox instances and manual snapshots created by the kit"
restoredrill cleanup || warn "cleanup reported a problem - check the RDS console"

if tf_has_resources "$TF"; then
  log "Terraform destroy"
  tf_destroy "$TF"
else
  log "Nothing in Terraform state - already down"
fi
log "Down. Nothing from restore-drill-kit is billing any more."
