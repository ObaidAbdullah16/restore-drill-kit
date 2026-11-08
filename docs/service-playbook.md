# Service S4 - Backup & Restore Drill

**Promise:** "Have you ever restored your backup? I'll prove it works in an isolated sandbox and time it."
**Founding price:** $149-$299 (India: Rs 9k-20k). 3-5 days.

## Why clients buy it

In April 2026 an AI coding agent deleted a SaaS company's production database *and its backups* in
seconds, because one credential could delete both. Most teams have backups; few have ever restored one,
and almost none know their real RTO (how long recovery takes) or RPO (how much data they would lose).

## Scope (fixed price)

1. **Inventory**: which databases, how they are backed up (RDS automated, snapshots, pg_dump to S3, AWS Backup).
2. **Isolation review** (`restoredrill isolation`): retention, deletion protection, copies in another
   account/region, locked vault, and whether the app/CI identity can delete backups (IAM policy simulation).
3. **Timed drill** in an isolated sandbox (its own security group, never production's): snapshot or point-in-time restore -> data checks -> delete sandbox.
4. **Report**: RTO and RPO achieved vs. targets, 13 data checks, isolation findings with fixes.
5. **Runbook**: the exact steps and commands, so their team can repeat it every quarter.

## Access needed

- AWS: a role in a **non-production** account or with permissions limited to RDS restore into a tagged
  sandbox (`rds:RestoreDBInstance*`, `rds:Describe*`, `rds:DeleteDBInstance` on `*-sandbox-*`), plus read
  of the backup manifest. No access to the production database itself.
- Data checks run against the **restored copy only**. For real customer data, agree on which checks are
  allowed (row counts and checksums reveal no personal data).

## Day by day

| Day | Work |
|---|---|
| 1 | Kick-off, inventory, isolation report |
| 2 | First drill (snapshot), fix blockers (missing SG rule, wrong subnet group...) |
| 3 | Second drill (point-in-time), compare RTO/RPO with targets |
| 4 | Report + runbook, walkthrough call |
| 5 | Optional: schedule quarterly drills (GitHub Actions `control.yml` with `drill`) |

## Upsell

- Quarterly drill retainer ($99-$199/quarter).
- Cross-account backup copy + AWS Backup Vault Lock setup.
