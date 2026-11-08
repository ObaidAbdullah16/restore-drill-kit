"""Backup-isolation checks: could one compromised identity (or a runaway AI agent)
delete production *and* its backups?"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any

from botocore.exceptions import ClientError

DESTRUCTIVE = [
    "rds:DeleteDBInstance",
    "rds:DeleteDBSnapshot",
    "rds:DeleteDBClusterSnapshot",
    "rds:DeleteDBInstanceAutomatedBackup",
    "rds:ModifyDBInstance",
    "backup:DeleteRecoveryPoint",
    "backup:DeleteBackupVault",
    "s3:DeleteObjectVersion",
    "s3:PutBucketVersioning",
    "s3:PutLifecycleConfiguration",
]


@dataclass
class IsolationFinding:
    check: str
    status: str   # pass | warn | fail | info
    detail: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def check_isolation(session: Any, source_id: str, copy_region: str | None = None,
                    app_role_arn: str | None = None, now: dt.datetime | None = None) -> list[IsolationFinding]:
    now = now or dt.datetime.now(dt.timezone.utc)
    rds = session.client("rds")
    out: list[IsolationFinding] = []
    inst = rds.describe_db_instances(DBInstanceIdentifier=source_id)["DBInstances"][0]

    days = inst.get("BackupRetentionPeriod", 0)
    if days >= 7:
        out.append(IsolationFinding("automated backup retention", "pass", f"{days} days"))
    elif days > 0:
        out.append(IsolationFinding("automated backup retention", "warn", f"only {days} day(s): a problem noticed on Monday may be unrecoverable",
                                    "Set BackupRetentionPeriod to at least 7 (35 for production)."))
    else:
        out.append(IsolationFinding("automated backup retention", "fail", "automated backups are disabled",
                                    "Enable automated backups (BackupRetentionPeriod >= 7)."))

    if inst.get("DeletionProtection"):
        out.append(IsolationFinding("deletion protection", "pass", "enabled on the source instance"))
    else:
        out.append(IsolationFinding("deletion protection", "warn", "disabled: one API call deletes the database",
                                    "aws rds modify-db-instance --deletion-protection --db-instance-identifier " + source_id))

    # Manual snapshots shared with other accounts. Sharing is NOT a copy: the snapshot still lives in this
    # account, and whoever deletes it here deletes it for everyone. Only a copy owned by the backup
    # account survives, and that copy can't be seen from here, so the best result is "info: verify it".
    snaps = rds.describe_db_snapshots(DBInstanceIdentifier=source_id, SnapshotType="manual")["DBSnapshots"]
    shared, public = [], []
    for s in snaps:
        attrs = rds.describe_db_snapshot_attributes(DBSnapshotIdentifier=s["DBSnapshotIdentifier"])
        for a in attrs["DBSnapshotAttributesResult"].get("DBSnapshotAttributes", []):
            if a.get("AttributeName") == "restore" and a.get("AttributeValues"):
                (public if "all" in a["AttributeValues"] else shared).append(s["DBSnapshotIdentifier"])
    if public:
        out.append(IsolationFinding("public snapshots", "fail",
                                    f"{', '.join(public)} can be restored by ANY AWS account (shared with 'all')",
                                    "Remove 'all' from the snapshot's restore attribute now: "
                                    "aws rds modify-db-snapshot-attribute --attribute-name restore --values-to-remove all "
                                    "--db-snapshot-identifier <id>"))
    if shared:
        out.append(IsolationFinding("copy in another account", "info",
                                    f"{len(shared)} snapshot(s) shared with another account; sharing is not a copy - "
                                    "deleting them here removes them for the backup account too",
                                    "Confirm the backup account COPIES each shared snapshot (or use AWS Backup cross-account copy)."))
    else:
        out.append(IsolationFinding("copy in another account", "warn",
                                    "no snapshot is shared with a separate backup account: whoever controls this account controls every copy",
                                    "Copy snapshots to a backup account (AWS Backup cross-account copy) with its own credentials."))

    # Copies in a second region
    if copy_region:
        other = session.client("rds", region_name=copy_region)
        fresh = []
        for s in other.describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]:
            # Only copies of THIS database, judged by when the data was captured: a copy's
            # SnapshotCreateTime is the copy time, OriginalSnapshotCreateTime is the data's age.
            created = s.get("OriginalSnapshotCreateTime") or s.get("SnapshotCreateTime")
            if (s.get("SourceRegion") and s.get("DBInstanceIdentifier") == source_id
                    and created and (now - created).days <= 7):
                fresh.append(s["DBSnapshotIdentifier"])
        try:
            replicated = other.describe_db_instance_automated_backups(DBInstanceIdentifier=source_id).get("DBInstanceAutomatedBackups", [])
        except ClientError:
            replicated = []
        if fresh or replicated:
            out.append(IsolationFinding("copy in another region", "pass", f"{len(fresh)} recent copies, {len(replicated)} replicated automated backup(s) in {copy_region}"))
        else:
            out.append(IsolationFinding("copy in another region", "warn", f"no copy from the last 7 days in {copy_region}",
                                        "Enable cross-region automated backup replication or copy snapshots on a schedule."))

    # AWS Backup vault lock (WORM)
    try:
        vaults = session.client("backup").list_backup_vaults().get("BackupVaultList", [])
        locked = [v["BackupVaultName"] for v in vaults if v.get("Locked")]
        if locked:
            out.append(IsolationFinding("immutable vault", "pass", "vault lock on: " + ", ".join(locked)))
        elif vaults:
            out.append(IsolationFinding("immutable vault", "warn", f"{len(vaults)} AWS Backup vault(s), none locked",
                                        "Enable AWS Backup Vault Lock (governance or compliance mode)."))
        else:
            out.append(IsolationFinding("immutable vault", "info", "AWS Backup is not used",
                                        "Consider AWS Backup with a locked vault for ransomware-resistant copies."))
    except ClientError as e:
        out.append(IsolationFinding("immutable vault", "info", f"could not list vaults: {e.response['Error']['Code']}"))

    # Can the application identity destroy the backups too?
    if app_role_arn:
        try:
            res = session.client("iam").simulate_principal_policy(PolicySourceArn=app_role_arn, ActionNames=DESTRUCTIVE)
            allowed = [r["EvalActionName"] for r in res.get("EvaluationResults", []) if r.get("EvalDecision") == "allowed"]
            if allowed:
                out.append(IsolationFinding("blast radius of the app identity", "fail",
                                            f"{app_role_arn.split('/')[-1]} is allowed: {', '.join(allowed)}",
                                            "Remove delete permissions on backups from application and CI roles; use a separate break-glass role."))
            else:
                out.append(IsolationFinding("blast radius of the app identity", "pass", "cannot delete or weaken backups"))
        except Exception as e:  # noqa: BLE001 - missing iam:SimulatePrincipalPolicy, or not supported by a simulator
            out.append(IsolationFinding("blast radius of the app identity", "info", f"simulation not possible: {e.__class__.__name__}"))
    return out
