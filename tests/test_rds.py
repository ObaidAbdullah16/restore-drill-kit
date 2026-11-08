import datetime as dt

import boto3
import pytest
from moto import mock_aws

from restoredrill.dataset import Manifest
from restoredrill.drill import TAG_KEY, cleanup_rds, drill_rds, snapshot_rds
from restoredrill.isolation import check_isolation
from restoredrill.verify import CheckResult

REGION = "ap-south-1"


@pytest.fixture
def aws(aws_env):
    with mock_aws():
        yield boto3.Session(region_name=REGION)


def _source(session, retention=1, protect=False):
    ec2, rds = session.client("ec2"), session.client("rds")
    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    subnets = [s["SubnetId"] for s in ec2.describe_subnets()["Subnets"][:2]]
    sg = ec2.create_security_group(GroupName="drill", Description="drill", VpcId=vpc)["GroupId"]
    rds.create_db_subnet_group(DBSubnetGroupName="drill", DBSubnetGroupDescription="d", SubnetIds=subnets)
    rds.create_db_instance(DBInstanceIdentifier="prod", DBInstanceClass="db.t4g.micro", Engine="postgres",
                           AllocatedStorage=20, MasterUsername="drill", MasterUserPassword="password123",
                           DBSubnetGroupName="drill", VpcSecurityGroupIds=[sg], BackupRetentionPeriod=retention,
                           DeletionProtection=protect)
    return sg


def test_snapshot_drill_creates_verifies_and_deletes_sandbox(aws):
    sg = _source(aws)
    snap = snapshot_rds(aws, "prod")
    seen = {}

    def fake_verify(conninfo, manifest):
        seen["conninfo"] = conninfo
        # ingress for this machine is open while verifying
        perms = aws.client("ec2").describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]["IpPermissions"]
        seen["open"] = any(r["CidrIp"] == "203.0.113.7/32" for p in perms for r in p.get("IpRanges", []))
        return [CheckResult("row count: customers", True, "10", "10")]

    m = Manifest(captured_at="2026-10-24T00:00:00+00:00", source="prod")
    res = drill_rds(aws, "prod", sg, "drill", m, "password123", verifier=fake_verify, ingress_ip="203.0.113.7",
                    rto_target=3600)
    assert res.passed and res.kind == "rds-snapshot" and snap in res.source
    assert "sslmode=require" in seen["conninfo"] and seen["open"]
    perms = aws.client("ec2").describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]["IpPermissions"]
    assert not any(r["CidrIp"] == "203.0.113.7/32" for p in perms for r in p.get("IpRanges", [])), "ingress must be revoked"
    names = [i["DBInstanceIdentifier"] for i in aws.client("rds").describe_db_instances()["DBInstances"]]
    assert names == ["prod"], "sandbox must be deleted after the drill"
    assert res.rpo_seconds >= 0 and "restore (RDS provisioning)" in res.phases


def test_failed_verification_fails_the_drill_and_still_cleans_up(aws):
    sg = _source(aws)
    snapshot_rds(aws, "prod")
    m = Manifest(captured_at="2026-10-24T00:00:00+00:00", source="prod")
    res = drill_rds(aws, "prod", sg, "drill", m, "x",
                    verifier=lambda c, mm: [CheckResult("checksum: orders", False, "abc", "def")], ingress_ip="203.0.113.7")
    assert not res.passed
    assert [i["DBInstanceIdentifier"] for i in aws.client("rds").describe_db_instances()["DBInstances"]] == ["prod"]


def test_cleanup_removes_tagged_resources_only(aws):
    _source(aws)
    snapshot_rds(aws, "prod")
    rds = aws.client("rds")
    rds.create_db_snapshot(DBSnapshotIdentifier="keep-me", DBInstanceIdentifier="prod")
    rds.restore_db_instance_from_db_snapshot(DBInstanceIdentifier="leftover", DBSnapshotIdentifier="keep-me",
                                             Tags=[{"Key": TAG_KEY, "Value": "sandbox"}])
    removed = cleanup_rds(aws, wait=False)
    assert "leftover" in removed and "keep-me" not in removed
    left = {s["DBSnapshotIdentifier"] for s in rds.describe_db_snapshots(SnapshotType="manual")["DBSnapshots"]}
    assert left == {"keep-me"}


def test_isolation_findings(aws):
    _source(aws, retention=1, protect=False)
    found = {f.check: f.status for f in check_isolation(aws, "prod", now=dt.datetime.now(dt.timezone.utc))}
    assert found["automated backup retention"] == "warn"
    assert found["deletion protection"] == "warn"
    assert found["copy in another account"] == "warn"


def test_sandbox_sg_is_opened_never_the_production_one(aws):
    prod_sg = _source(aws)
    ec2 = aws.client("ec2")
    vpc = ec2.describe_vpcs()["Vpcs"][0]["VpcId"]
    sandbox_sg = ec2.create_security_group(GroupName="sandbox", Description="sandbox", VpcId=vpc)["GroupId"]
    snapshot_rds(aws, "prod")
    opened = {}

    def fake_verify(conninfo, manifest):
        for name, sg in (("prod", prod_sg), ("sandbox", sandbox_sg)):
            perms = ec2.describe_security_groups(GroupIds=[sg])["SecurityGroups"][0]["IpPermissions"]
            opened[name] = any(p.get("IpRanges") for p in perms)
        return [CheckResult("row count: customers", True, "1", "1")]

    m = Manifest(captured_at="2026-10-24T00:00:00+00:00", source="prod")
    assert drill_rds(aws, "prod", sandbox_sg, "drill", m, "pw", verifier=fake_verify, ingress_ip="203.0.113.7").passed
    assert opened == {"prod": False, "sandbox": True}


def test_unverifiable_restore_is_a_failed_drill_with_a_report(aws):
    sg = _source(aws)
    snapshot_rds(aws, "prod")

    def unreachable(conninfo, manifest):
        raise TimeoutError("connection timed out")

    m = Manifest(captured_at="2026-10-24T00:00:00+00:00", source="prod")
    res = drill_rds(aws, "prod", sg, "drill", m, "pw", verifier=unreachable, ingress_ip="203.0.113.7")
    assert not res.passed
    assert res.checks[0].name == "restore and verify" and "TimeoutError" in res.checks[0].actual
    assert res.rto_seconds > 0
    assert [i["DBInstanceIdentifier"] for i in aws.client("rds").describe_db_instances()["DBInstances"]] == ["prod"]


def test_public_and_shared_snapshots_are_not_backups(aws):
    _source(aws)
    rds = aws.client("rds")
    rds.create_db_snapshot(DBSnapshotIdentifier="leaky", DBInstanceIdentifier="prod")
    rds.modify_db_snapshot_attribute(DBSnapshotIdentifier="leaky", AttributeName="restore", ValuesToAdd=["all"])
    rds.create_db_snapshot(DBSnapshotIdentifier="shared", DBInstanceIdentifier="prod")
    rds.modify_db_snapshot_attribute(DBSnapshotIdentifier="shared", AttributeName="restore", ValuesToAdd=["210987654321"])
    found = {f.check: f for f in check_isolation(aws, "prod")}
    assert found["public snapshots"].status == "fail" and "leaky" in found["public snapshots"].detail
    # sharing is reported, but it is not a copy, so it can't "pass"
    assert found["copy in another account"].status == "info"
