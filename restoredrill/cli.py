"""restoredrill command line.

Local (Docker or any two Postgres servers):
    restoredrill seed   --dsn postgresql://postgres:postgres@localhost:5433/shop
    restoredrill backup --dsn postgresql://postgres:postgres@localhost:5433/shop --out backups/
    restoredrill drill  --backup backups/ --sandbox-dsn postgresql://postgres:postgres@localhost:5434/postgres --report reports/local

AWS RDS:
    restoredrill rds-prepare --source-id restore-drill-prod --dsn ... --manifest s3://bucket/manifests/latest.json
    restoredrill rds-drill   --source-id restore-drill-prod --sg-id sg-... --subnet-group ... --manifest s3://... --mode snapshot
    restoredrill isolation   --source-id restore-drill-prod --copy-region us-east-1 --app-role-arn arn:aws:iam::...:role/app
    restoredrill cleanup
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from . import __version__


def duration(text: str) -> float:
    """'90s', '30m', '4h', '1d', '1h30m' -> seconds."""
    total, matched = 0.0, False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([smhd])", text.lower()):
        total += float(num) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        matched = True
    if not matched:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (use e.g. 30m, 4h, 1d)")
    return total


def _session(region: str | None):
    import boto3
    return boto3.Session(region_name=region or os.environ.get("AWS_REGION") or "ap-southeast-2")


def _password(args: argparse.Namespace, session) -> str:
    if os.environ.get("RESTORE_DRILL_DB_PASSWORD"):
        return os.environ["RESTORE_DRILL_DB_PASSWORD"]
    return session.client("ssm").get_parameter(Name=args.password_ssm, WithDecryption=True)["Parameter"]["Value"]


def _print_result(res, files) -> None:
    from .report import human
    print(f"{'PASSED' if res.passed else 'FAILED'}: RTO {human(res.rto_seconds)}, RPO {human(res.rpo_seconds)}, "
          f"checks {sum(c.ok for c in res.checks)}/{len(res.checks)}")
    for f in files:
        print(f"  wrote {f}")


def cmd_seed(a: argparse.Namespace) -> int:
    from .dataset import load
    counts = load(a.dsn, customers=a.customers, seed=a.seed)
    print("loaded synthetic data:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def cmd_backup(a: argparse.Namespace) -> int:
    from .drill import backup_local
    dump, manifest = backup_local(a.dsn, a.out, label=a.label)
    print(f"backup {dump}\nmanifest {manifest}")
    return 0


def cmd_drill(a: argparse.Namespace) -> int:
    from .drill import drill_local
    from .report import write_all
    res = drill_local(a.backup, a.sandbox_dsn, keep=a.keep, rto_target=a.rto_target, rpo_target=a.rpo_target)
    res.synthetic = not a.real_data
    _print_result(res, write_all(res, a.report))
    return 0 if res.passed else 1


def cmd_rds_prepare(a: argparse.Namespace) -> int:
    from .dataset import capture, load
    from .drill import TemporaryIngress, save_manifest, snapshot_rds
    session = _session(a.region)
    with TemporaryIngress(session.client("ec2"), a.sg_id):
        if a.seed:
            counts = load(a.dsn, customers=a.customers)
            print("seeded:", counts)
        manifest = capture(a.dsn, a.source_id)
    sid = snapshot_rds(session, a.source_id)
    manifest.source = f"{a.source_id} (snapshot {sid})"
    save_manifest(manifest, a.manifest, session.client("s3"))
    print(f"snapshot {sid} ready, manifest saved to {a.manifest}")
    return 0


def cmd_rds_drill(a: argparse.Namespace) -> int:
    from .drill import drill_rds, load_manifest
    from .isolation import check_isolation
    from .report import write_all
    session = _session(a.region)
    manifest = load_manifest(a.manifest, session.client("s3"))
    res = drill_rds(session, a.source_id, a.sg_id, a.subnet_group, manifest, _password(a, session), mode=a.mode,
                    instance_class=a.instance_class, keep=a.keep, rto_target=a.rto_target, rpo_target=a.rpo_target)
    iso = check_isolation(session, a.source_id, a.copy_region, a.app_role_arn) if a.with_isolation else None
    _print_result(res, write_all(res, a.report, iso))
    return 0 if res.passed else 1


def cmd_isolation(a: argparse.Namespace) -> int:
    import json

    from .isolation import check_isolation
    findings = check_isolation(_session(a.region), a.source_id, a.copy_region, a.app_role_arn)
    for f in findings:
        print(f"[{f.status.upper():4}] {f.check}: {f.detail}")
    if a.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    return 1 if any(f.status == "fail" for f in findings) else 0


def cmd_cleanup(a: argparse.Namespace) -> int:
    from .drill import cleanup_rds
    removed = cleanup_rds(_session(a.region), wait=not a.no_wait)
    print("removed:", ", ".join(removed) or "nothing")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="restoredrill", description="Prove your Postgres backups restore, and time it.")
    ap.add_argument("--version", action="version", version=f"restoredrill {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def targets(p: argparse.ArgumentParser) -> None:
        p.add_argument("--rto-target", type=duration, help="e.g. 30m - the drill fails if restore+verify takes longer")
        p.add_argument("--rpo-target", type=duration, help="e.g. 24h - the drill fails if the backup is older")
        p.add_argument("--report", default="reports/latest", help="report folder")
        p.add_argument("--keep", action="store_true", help="keep the restored database for inspection")

    s = sub.add_parser("seed", help="load the synthetic shop dataset")
    s.add_argument("--dsn", required=True)
    s.add_argument("--customers", type=int, default=500)
    s.add_argument("--seed", type=int, default=42)
    s.set_defaults(func=cmd_seed)

    b = sub.add_parser("backup", help="pg_dump + manifest")
    b.add_argument("--dsn", required=True)
    b.add_argument("--out", default="backups")
    b.add_argument("--label", default="local source")
    b.set_defaults(func=cmd_backup)

    d = sub.add_parser("drill", help="restore the latest local backup into a fresh database and verify it")
    d.add_argument("--backup", default="backups", help="backup folder (uses LATEST) or a .dump file")
    d.add_argument("--sandbox-dsn", required=True, help="admin connection to the sandbox server")
    d.add_argument("--real-data", action="store_true", help="omit the synthetic-data banner")
    targets(d)
    d.set_defaults(func=cmd_drill)

    rp = sub.add_parser("rds-prepare", help="(optionally seed) capture manifest and snapshot the RDS source")
    rp.add_argument("--source-id", required=True)
    rp.add_argument("--dsn", required=True)
    rp.add_argument("--sg-id", required=True, help="security group to open temporarily for this machine")
    rp.add_argument("--manifest", required=True, help="s3://bucket/key or local path")
    rp.add_argument("--seed", action="store_true")
    rp.add_argument("--customers", type=int, default=2000)
    rp.add_argument("--region")
    rp.set_defaults(func=cmd_rds_prepare)

    rd = sub.add_parser("rds-drill", help="restore RDS into a sandbox instance, verify, delete")
    rd.add_argument("--source-id", required=True)
    rd.add_argument("--sg-id", required=True, help="security group for the sandbox (NOT the production database's)")
    rd.add_argument("--subnet-group", required=True)
    rd.add_argument("--manifest", required=True)
    rd.add_argument("--mode", choices=["snapshot", "pitr"], default="snapshot")
    rd.add_argument("--instance-class", default="db.t4g.micro")
    rd.add_argument("--password-ssm", default="/restore-drill/db-password")
    rd.add_argument("--with-isolation", action="store_true", help="add the backup-isolation checks to the report")
    rd.add_argument("--copy-region")
    rd.add_argument("--app-role-arn")
    rd.add_argument("--region")
    targets(rd)
    rd.set_defaults(func=cmd_rds_drill)

    i = sub.add_parser("isolation", help="backup-isolation checks only")
    i.add_argument("--source-id", required=True)
    i.add_argument("--copy-region")
    i.add_argument("--app-role-arn")
    i.add_argument("--json", action="store_true")
    i.add_argument("--region")
    i.set_defaults(func=cmd_isolation)

    c = sub.add_parser("cleanup", help="delete sandbox instances and manual snapshots created by this kit")
    c.add_argument("--region")
    c.add_argument("--no-wait", action="store_true")
    c.set_defaults(func=cmd_cleanup)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
