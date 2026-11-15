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


# ------------------------------------------------------------------ AWS Free plan, retries, partial failures
class _Waiter:
    def __init__(self, log):
        self.log = log

    def wait(self, DBInstanceIdentifier, WaiterConfig):  # noqa: N803 - boto3 argument name
        self.log.append(DBInstanceIdentifier)


class _FakeRDS:
    """Just enough RDS for the code paths moto cannot reach ('deleting' state, Free-plan quota)."""

    def __init__(self, instances):
        self.instances, self.waited, self.deleted = instances, [], []

    def describe_db_instances(self, DBInstanceIdentifier=None):  # noqa: N803
        found = [i for i in self.instances if DBInstanceIdentifier in (None, i["DBInstanceIdentifier"])]
        return {"DBInstances": found}

    def delete_db_instance(self, DBInstanceIdentifier, **kw):  # noqa: N803
        self.deleted.append(DBInstanceIdentifier)

    def get_waiter(self, name):
        return _Waiter(self.waited)

    def describe_db_snapshots(self, DBInstanceIdentifier):  # noqa: N803
        return {"DBSnapshots": [{"DBSnapshotIdentifier": "s1", "Status": "available",
                                 "SnapshotCreateTime": dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)}]}

    def restore_db_instance_from_db_snapshot(self, **kw):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "InstanceQuotaExceeded", "Message": "free plan"}}, "RestoreDBInstanceFromDBSnapshot")


class _FakeSession:
    def __init__(self, rds):
        self.rds = rds

    def client(self, name):
        return self.rds


def _inst(name, status, sandbox):
    tags = [{"Key": TAG_KEY, "Value": "sandbox"}] if sandbox else []
    return {"DBInstanceIdentifier": name, "DBInstanceStatus": status, "TagList": tags, "MasterUsername": "drill"}


def test_old_sandboxes_are_removed_before_a_new_restore():
    from restoredrill.drill import clear_old_sandboxes
    rds = _FakeRDS([_inst("prod", "available", False), _inst("prod-sandbox-1", "deleting", True),
                    _inst("other-db", "available", False), _inst("prod-sandbox-2", "available", True)])
    assert clear_old_sandboxes(rds) == ["prod-sandbox-1", "prod-sandbox-2"]
    assert rds.deleted == ["prod-sandbox-2"], "a leftover sandbox is deleted; one already deleting is not asked again"
    assert rds.waited == ["prod-sandbox-1", "prod-sandbox-2"], "and the drill waits until both are gone"
    assert "prod" not in rds.deleted and "other-db" not in rds.deleted, "never touches other databases"


def test_free_plan_instance_quota_is_explained():
    m = Manifest(captured_at="2026-11-15T00:00:00+00:00", source="prod")
    alone = _FakeRDS([_inst("prod", "available", False)])
    with pytest.raises(RuntimeError, match="may not run a second database"):
        drill_rds(_FakeSession(alone), "prod", "sg-1", "drill", m, "pw")
    crowded = _FakeRDS([_inst("prod", "available", False), _inst("shipit-db", "available", False)])
    with pytest.raises(RuntimeError, match=r"Besides prod there is: shipit-db \(available\)"):
        drill_rds(_FakeSession(crowded), "prod", "sg-1", "drill", m, "pw")


def test_retry_repeats_network_and_credential_errors_only():
    from botocore.exceptions import CredentialRetrievalError, EndpointConnectionError

    from restoredrill.drill import retry
    calls, slept = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise EndpointConnectionError(endpoint_url="https://ap-southeast-2.signin.aws.amazon.com/v1/token")
        return "ok"

    assert retry(flaky, sleep=slept.append) == "ok" and len(slept) == 2
    with pytest.raises(CredentialRetrievalError):
        retry(lambda: (_ for _ in ()).throw(CredentialRetrievalError(provider="custom-process", error_msg="x")),
              attempts=2, sleep=slept.append)
    with pytest.raises(ValueError):
        retry(lambda: (_ for _ in ()).throw(ValueError("not transient")), sleep=slept.append)


def test_report_is_written_even_if_the_isolation_check_fails(monkeypatch, tmp_path):
    import restoredrill.cli as cli
    import restoredrill.drill as drill
    import restoredrill.isolation as isolation
    from restoredrill.drill import DrillResult

    res = DrillResult(kind="rds-snapshot", source="snapshot s1", restore_point="2026-11-15T00:00:00+00:00",
                      started_at="2026-11-15T01:00:00+00:00", checks=[CheckResult("row count: customers", True, "10", "10")])

    def broken(*a, **k):
        raise RuntimeError("Could not connect to the endpoint URL")

    monkeypatch.setattr(cli, "_session", lambda region: None)
    monkeypatch.setattr(cli, "_password", lambda a, s: "pw")
    monkeypatch.setattr(drill, "load_manifest", lambda uri, s3: Manifest(captured_at="x", source="prod"))
    monkeypatch.setattr(drill, "drill_rds", lambda *a, **k: res)
    monkeypatch.setattr(isolation, "check_isolation", broken)
    monkeypatch.setattr(cli, "_session", lambda region: _FakeSession(None))
    rc = cli.main(["rds-drill", "--source-id", "prod", "--sg-id", "sg-1", "--subnet-group", "g", "--manifest", "s3://b/m.json",
                   "--with-isolation", "--report", str(tmp_path / "r")])
    assert rc == 0 and (tmp_path / "r" / "report.html").exists()
    assert any("isolation checks skipped" in n for n in res.notes)
