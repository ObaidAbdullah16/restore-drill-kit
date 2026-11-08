#!/usr/bin/env bash
# Shared helpers for this repo's up/down/status scripts. Sourced, not executed.
# Settings come from the environment, or from ../_setup/config.env when this repo
# sits inside the Projects_Demos folder.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$REPO_ROOT/../_setup/config.env" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/../_setup/config.env"
fi
: "${AWS_REGION:=ap-southeast-2}"
: "${AUTO_APPROVE:=1}"          # set AUTO_APPROVE=0 to review the plan before applying
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"
[[ -n "${AWS_PROFILE:-}" ]] && export AWS_PROFILE

log()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

need() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "'$c' is not installed (see _setup/install-tools-wsl.sh)"
  done
}

aws_account_id() { aws sts get-caller-identity --query Account --output text; }

state_bucket() { echo "${TF_STATE_BUCKET:-tfstate-$(aws_account_id)-${AWS_REGION}}"; }

# tf_init <terraform dir> <state key>
tf_init() {
  local dir="$1" key="$2" bucket
  bucket="$(state_bucket)"
  aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 \
    || die "State bucket $bucket not found: the AWS foundation is not set up (or you are logged in to another account). Start it first: bash _setup/projects.sh up foundation"
  terraform -chdir="$dir" init -input=false -reconfigure \
    -backend-config="bucket=${bucket}" \
    -backend-config="key=${key}" \
    -backend-config="region=${AWS_REGION}" \
    -backend-config="use_lockfile=true" \
    -backend-config="encrypt=true" >/dev/null
}

tf_apply() { # tf_apply <dir> [extra args...]
  local dir="$1"; shift
  if [[ "$AUTO_APPROVE" == "1" ]]; then
    terraform -chdir="$dir" apply -input=false -auto-approve -var "aws_region=${AWS_REGION}" "$@"
  else
    terraform -chdir="$dir" apply -input=false -var "aws_region=${AWS_REGION}" "$@"
  fi
}

tf_destroy() { # tf_destroy <dir> [extra args...]
  local dir="$1"; shift
  if [[ "$AUTO_APPROVE" == "1" ]]; then
    terraform -chdir="$dir" destroy -input=false -auto-approve -var "aws_region=${AWS_REGION}" "$@"
  else
    terraform -chdir="$dir" destroy -input=false -var "aws_region=${AWS_REGION}" "$@"
  fi
}

tf_out() { terraform -chdir="$1" output -raw "$2" 2>/dev/null || true; }

# Is anything in this Terraform state? (used by status / auto-stop)
tf_has_resources() { [[ -n "$(terraform -chdir="$1" state list 2>/dev/null | grep -v '^data\.' || true)" ]]; }

# The restoredrill CLI: installed into this repo's .venv the first time (Ubuntu 24.04 does not
# allow pip installs into the system Python). In GitHub Actions setup-python is used instead.
ensure_restoredrill() {
  command -v restoredrill >/dev/null 2>&1 && return 0
  if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
    python3 -m pip install -q -e "$REPO_ROOT"
    return 0
  fi
  if [[ ! -x "$REPO_ROOT/.venv/bin/restoredrill" ]]; then
    log "Installing the restoredrill tool into $REPO_ROOT/.venv (first time only)"
    python3 -m venv "$REPO_ROOT/.venv" || die "python3-venv is missing: sudo apt-get install -y python3-venv"
    "$REPO_ROOT/.venv/bin/pip" install -q -e "$REPO_ROOT"
  fi
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
}
