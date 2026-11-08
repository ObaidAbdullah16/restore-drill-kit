import json

import psycopg
import pytest
from conftest import PROD, SANDBOX, needs_pg

from restoredrill.cli import main
from restoredrill.dataset import Manifest, capture, load
from restoredrill.drill import backup_local, drill_local


@needs_pg
def test_full_local_drill(tmp_path):
    counts = load(PROD, customers=300)
    dump, mpath = backup_local(PROD, str(tmp_path / "backups"))
    m = Manifest.from_json(mpath.read_text())
    assert m.row_counts == counts
    res = drill_local(str(tmp_path / "backups"), SANDBOX, rto_target=600, rpo_target=3600)
    assert res.passed, [c for c in res.checks if not c.ok]
    assert len(res.checks) == 13
    # the drill database is dropped afterwards
    with psycopg.connect(SANDBOX) as conn:
        names = [r[0] for r in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'drill_%'")]
    assert names == []


@needs_pg
def test_drill_detects_a_bad_backup(tmp_path):
    load(PROD, customers=200)
    _, mpath = backup_local(PROD, str(tmp_path / "b"))
    # Pretend the backup should contain more data than it does (e.g. a dump taken mid-incident)
    m = json.loads(mpath.read_text())
    m["row_counts"]["orders"] += 5
    m["checksums"]["payments"] = "deadbeef"
    mpath.write_text(json.dumps(m))
    res = drill_local(str(tmp_path / "b"), SANDBOX)
    failed = {c.name for c in res.checks if not c.ok}
    assert failed == {"row count: orders", "checksum: payments"}
    assert not res.passed


@needs_pg
def test_checksum_changes_when_data_changes():
    load(PROD, customers=50)
    before = capture(PROD)
    with psycopg.connect(PROD) as conn:
        conn.execute("UPDATE customers SET city = 'Bhopal' WHERE id = 1")
    after = capture(PROD)
    assert before.checksums["customers"] != after.checksums["customers"]
    assert before.checksums["orders"] == after.checksums["orders"]


@needs_pg
def test_cli_end_to_end(tmp_path):
    assert main(["seed", "--dsn", PROD, "--customers", "100"]) == 0
    assert main(["backup", "--dsn", PROD, "--out", str(tmp_path / "bk")]) == 0
    code = main(["drill", "--backup", str(tmp_path / "bk"), "--sandbox-dsn", SANDBOX,
                 "--rto-target", "10m", "--report", str(tmp_path / "rep")])
    assert code == 0
    data = json.loads((tmp_path / "rep" / "drill.json").read_text())
    assert data["passed"] and data["synthetic_data"]


@needs_pg
def test_corrupt_backup_gives_a_failed_report_not_a_crash(tmp_path):
    load(PROD, customers=50)
    dump, _ = backup_local(PROD, str(tmp_path / "c"))
    dump.write_bytes(dump.read_bytes()[:200])   # a truncated dump file
    res = drill_local(str(tmp_path / "c"), SANDBOX)
    assert not res.passed
    assert res.checks[0].name == "restore and verify" and "CalledProcessError" in res.checks[0].actual
    with psycopg.connect(SANDBOX) as conn:
        assert not list(conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'drill_%'"))


def test_missing_pg_tools_message(monkeypatch, tmp_path):
    from restoredrill import drill
    monkeypatch.setattr(drill.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="postgresql-client"):
        drill._need("pg_dump")
