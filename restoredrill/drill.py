"""Backup and restore drills: local (pg_dump/pg_restore) and AWS RDS (snapshot or point-in-time)."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from .dataset import Manifest, capture
from .verify import CheckResult, verify


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class DrillResult:
    kind: str                       # "local" or "rds-snapshot" / "rds-pitr"
    source: str
    restore_point: str              # ISO time the restored data represents
    started_at: str
    phases: dict[str, float] = field(default_factory=dict)   # seconds
    checks: list[CheckResult] = field(default_factory=list)
    rto_seconds: float = 0.0
    rpo_seconds: float = 0.0
    rto_target_seconds: float | None = None
    rpo_target_seconds: float | None = None
    target: str = ""
    notes: list[str] = field(default_factory=list)
    synthetic: bool = True

    @property
    def data_ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def rto_ok(self) -> bool | None:
        return None if self.rto_target_seconds is None else self.rto_seconds <= self.rto_target_seconds

    @property
    def rpo_ok(self) -> bool | None:
        return None if self.rpo_target_seconds is None else self.rpo_seconds <= self.rpo_target_seconds

    @property
    def passed(self) -> bool:
        return self.data_ok and self.rto_ok is not False and self.rpo_ok is not False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "source": self.source, "target": self.target,
            "restore_point": self.restore_point, "started_at": self.started_at,
            "phases_seconds": {k: round(v, 1) for k, v in self.phases.items()},
            "rto_seconds": round(self.rto_seconds, 1), "rpo_seconds": round(self.rpo_seconds, 1),
            "rto_target_seconds": self.rto_target_seconds, "rpo_target_seconds": self.rpo_target_seconds,
            "data_ok": self.data_ok, "rto_ok": self.rto_ok, "rpo_ok": self.rpo_ok, "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks], "notes": self.notes, "synthetic_data": self.synthetic,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _failure(name: str, e: Exception) -> CheckResult:
    detail = getattr(e, "stderr", None) or str(e)
    return CheckResult(name, False, "restored database reachable and all checks run",
                       f"{type(e).__name__}: {' '.join(str(detail).split())[:300]}")


# --------------------------------------------------------------------- local

def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"{tool} not found - install the PostgreSQL client tools (postgresql-client)")
    return path


def backup_local(conninfo: str, out_dir: str, label: str = "local") -> tuple[Path, Path]:
    """pg_dump in custom format + a manifest captured just before the dump."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    manifest = capture(conninfo, label)
    dump = out / f"{stamp}.dump"
    subprocess.run([_need("pg_dump"), "--format=custom", "--no-owner", "--no-privileges",
                    f"--file={dump}", f"--dbname={conninfo}"], check=True)
    mpath = out / f"{stamp}.manifest.json"
    mpath.write_text(manifest.to_json(), encoding="utf-8")
    (out / "LATEST").write_text(stamp + "\n", encoding="utf-8")
    return dump, mpath


def latest_backup(path: str) -> tuple[Path, Path]:
    p = Path(path)
    if p.is_dir():
        stamp = (p / "LATEST").read_text().strip()
        return p / f"{stamp}.dump", p / f"{stamp}.manifest.json"
    return p, p.with_name(p.name.replace(".dump", ".manifest.json"))


def drill_local(backup: str, sandbox_conninfo: str, keep: bool = False,
                rto_target: float | None = None, rpo_target: float | None = None) -> DrillResult:
    """Restore the latest dump into a brand-new database on the sandbox server and verify it."""
    dump, mpath = latest_backup(backup)
    manifest = Manifest.from_json(mpath.read_text())
    t0 = time.monotonic()
    start = utcnow()
    dbname = "drill_" + start.strftime("%Y%m%d_%H%M%S")
    res = DrillResult(kind="local", source=manifest.source or str(dump), restore_point=manifest.captured_at,
                      started_at=start.isoformat(timespec="seconds"), rto_target_seconds=rto_target,
                      rpo_target_seconds=rpo_target, target=dbname)
    admin = make_conninfo(sandbox_conninfo, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    target = make_conninfo(sandbox_conninfo, dbname=dbname)
    res.rpo_seconds = (start - dt.datetime.fromisoformat(manifest.captured_at)).total_seconds()
    try:
        t1 = t0
        try:
            subprocess.run([_need("pg_restore"), "--no-owner", "--no-privileges", "--exit-on-error",
                            f"--dbname={target}", str(dump)], check=True, capture_output=True, text=True)
            t1 = time.monotonic()
            res.phases["restore (pg_restore)"] = t1 - t0
            res.checks = verify(target, manifest)
        except (subprocess.CalledProcessError, psycopg.Error) as e:
            # A restore that fails IS the drill's finding: report it instead of crashing.
            res.checks.append(_failure("restore and verify", e))
        t2 = time.monotonic()
        res.phases["verify data" if res.phases else "failed attempt"] = t2 - t1
        res.rto_seconds = t2 - t0
    finally:
        if keep:
            res.notes.append(f"database {dbname} kept for inspection")
        else:
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    return res


# ----------------------------------------------------------------------- RDS

TAG_KEY = "restore-drill"


def my_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as r:
        return r.read().decode().strip()


class TemporaryIngress:
    """Opens port 5432 on a security group for this machine's IP and closes it again."""

    def __init__(self, ec2: Any, sg_id: str, ip: str | None = None, port: int = 5432):
        self.ec2, self.sg_id, self.port = ec2, sg_id, port
        self.cidr = f"{ip or my_ip()}/32"
        self.added = False

    def __enter__(self) -> TemporaryIngress:
        try:
            self.ec2.authorize_security_group_ingress(GroupId=self.sg_id, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": self.port, "ToPort": self.port,
                "IpRanges": [{"CidrIp": self.cidr, "Description": "restore-drill temporary access"}]}])
            self.added = True
        except Exception as e:  # noqa: BLE001
            if "Duplicate" not in str(e):
                raise
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.added:
            self.ec2.revoke_security_group_ingress(GroupId=self.sg_id, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": self.port, "ToPort": self.port, "IpRanges": [{"CidrIp": self.cidr}]}])


def load_manifest(location: str, s3: Any = None) -> Manifest:
    if location.startswith("s3://"):
        bucket, key = location[5:].split("/", 1)
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        return Manifest.from_json(body)
    return Manifest.from_json(Path(location).read_text())


def save_manifest(manifest: Manifest, location: str, s3: Any = None) -> None:
    if location.startswith("s3://"):
        bucket, key = location[5:].split("/", 1)
        s3.put_object(Bucket=bucket, Key=key, Body=manifest.to_json().encode(), ContentType="application/json")
    else:
        Path(location).write_text(manifest.to_json())


def snapshot_rds(session: Any, source_id: str, wait: bool = True) -> str:
    """Manual snapshot of the source (tagged so cleanup can find it)."""
    rds = session.client("rds")
    sid = f"{source_id}-drill-{utcnow():%Y%m%d%H%M%S}"
    rds.create_db_snapshot(DBSnapshotIdentifier=sid, DBInstanceIdentifier=source_id,
                           Tags=[{"Key": TAG_KEY, "Value": "manual-snapshot"}])
    if wait:
        rds.get_waiter("db_snapshot_available").wait(DBSnapshotIdentifier=sid, WaiterConfig={"Delay": 20, "MaxAttempts": 90})
    return sid


def drill_rds(session: Any, source_id: str, sg_id: str, subnet_group: str, manifest: Manifest,
              password: str, mode: str = "snapshot", instance_class: str = "db.t4g.micro",
              keep: bool = False, rto_target: float | None = None, rpo_target: float | None = None,
              verifier: Callable[[str, Manifest], list[CheckResult]] = verify,
              ingress_ip: str | None = None) -> DrillResult:
    rds, ec2 = session.client("rds"), session.client("ec2")
    t0 = time.monotonic()
    start = utcnow()
    sandbox = f"{source_id}-sandbox-{start:%m%d%H%M%S}"
    common = dict(DBInstanceClass=instance_class, DBSubnetGroupName=subnet_group, VpcSecurityGroupIds=[sg_id],
                  PubliclyAccessible=True, MultiAZ=False, DeletionProtection=False,
                  Tags=[{"Key": TAG_KEY, "Value": "sandbox"}])
    src = rds.describe_db_instances(DBInstanceIdentifier=source_id)["DBInstances"][0]
    if mode == "pitr":
        point = src["LatestRestorableTime"]
        rds.restore_db_instance_to_point_in_time(SourceDBInstanceIdentifier=source_id, TargetDBInstanceIdentifier=sandbox,
                                                 UseLatestRestorableTime=True, **common)
        source_desc = f"{source_id} at latest restorable time"
    else:
        snaps = rds.describe_db_snapshots(DBInstanceIdentifier=source_id)["DBSnapshots"]
        snaps = [s for s in snaps if s.get("Status") == "available"]
        if not snaps:
            raise RuntimeError(f"no available snapshots for {source_id}")
        snap = max(snaps, key=lambda s: s["SnapshotCreateTime"])
        point = snap["SnapshotCreateTime"]
        rds.restore_db_instance_from_db_snapshot(DBInstanceIdentifier=sandbox, DBSnapshotIdentifier=snap["DBSnapshotIdentifier"], **common)
        source_desc = f"snapshot {snap['DBSnapshotIdentifier']}"
    if point.tzinfo is None:
        point = point.replace(tzinfo=dt.timezone.utc)
    res = DrillResult(kind=f"rds-{mode}", source=source_desc, restore_point=point.isoformat(timespec="seconds"),
                      started_at=start.isoformat(timespec="seconds"), rto_target_seconds=rto_target,
                      rpo_target_seconds=rpo_target, target=sandbox, synthetic=True)
    res.rpo_seconds = (start - point).total_seconds()
    try:
        t1 = t0
        try:
            rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=sandbox, WaiterConfig={"Delay": 30, "MaxAttempts": 90})
            t1 = time.monotonic()
            res.phases["restore (RDS provisioning)"] = t1 - t0
            inst = rds.describe_db_instances(DBInstanceIdentifier=sandbox)["DBInstances"][0]
            host, port = inst["Endpoint"]["Address"], inst["Endpoint"]["Port"]
            conninfo = make_conninfo(host=host, port=port, user=src["MasterUsername"], password=password,
                                     dbname=src.get("DBName") or "postgres", sslmode="require", connect_timeout=15)
            with TemporaryIngress(ec2, sg_id, ingress_ip):
                res.checks = verifier(conninfo, manifest)
        except Exception as e:  # noqa: BLE001 - a restore that times out or can't be verified is a FAILED drill, with a report
            res.checks.append(_failure("restore and verify", e))
        t2 = time.monotonic()
        res.phases["verify data" if res.phases else "failed attempt"] = t2 - t1
        res.rto_seconds = t2 - t0
    finally:
        if keep:
            res.notes.append(f"sandbox {sandbox} kept - delete it to stop paying (bash scripts/down.sh does)")
        else:
            try:
                rds.delete_db_instance(DBInstanceIdentifier=sandbox, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)
                res.notes.append(f"sandbox {sandbox} deletion requested")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"could not delete sandbox {sandbox}: {e} - run scripts/down.sh")
    return res


def cleanup_rds(session: Any, wait: bool = True) -> list[str]:
    """Delete sandbox instances and manual snapshots created by this kit (found by tag)."""
    rds = session.client("rds")
    done: list[str] = []
    pending: list[str] = []  # every tagged sandbox, including ones a drill already asked to delete
    for inst in rds.describe_db_instances()["DBInstances"]:
        tags = {t["Key"]: t["Value"] for t in inst.get("TagList", [])}
        if tags.get(TAG_KEY) != "sandbox":
            continue
        name = inst["DBInstanceIdentifier"]
        if inst["DBInstanceStatus"] != "deleting":
            rds.delete_db_instance(DBInstanceIdentifier=name, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)
            done.append(name)
        pending.append(name)
    if wait:
        # Wait for all of them: while a sandbox still exists, its network interface keeps the security
        # group and subnet group in use, and `terraform destroy` would fail half-way.
        for name in pending:
            rds.get_waiter("db_instance_deleted").wait(DBInstanceIdentifier=name, WaiterConfig={"Delay": 30, "MaxAttempts": 60})
    for snap in rds.describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]:
        tags = {t["Key"]: t["Value"] for t in rds.list_tags_for_resource(ResourceName=snap["DBSnapshotArn"]).get("TagList", [])}
        if tags.get(TAG_KEY) == "manual-snapshot":
            rds.delete_db_snapshot(DBSnapshotIdentifier=snap["DBSnapshotIdentifier"])
            done.append(snap["DBSnapshotIdentifier"])
    return done
