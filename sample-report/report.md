# Restore drill report - PASSED

> Synthetic demo data (generated customers/orders). No real customer data.

- Drill type: **local**, started 2026-10-24T18:21:49+00:00
- Restored from: local source (data as of 2026-10-24T18:21:48+00:00)
- Restored into: `drill_20261024_182149` (deleted after the drill unless kept)
- **RTO achieved: 0.3s** (target: 5m 00s, met)
- **RPO at drill time: 1.2s** (target: 24h 00m, met)
- Data checks: 13/13 passed

## Timeline

| Phase | Duration |
|---|---|
| restore (pg_restore) | 0.2s |
| verify data | 0.1s |

## Data checks

| Check | Result | Expected | Actual |
|---|---|---|---|
| row count: customers | pass | `5000` | `5000` |
| row count: orders | pass | `14791` | `14791` |
| row count: order_items | pass | `37101` | `37101` |
| row count: payments | pass | `12802` | `12802` |
| checksum: customers | pass | `4175751ff090` | `4175751ff090` |
| checksum: orders | pass | `d61333150d20` | `d61333150d20` |
| checksum: order_items | pass | `e70841fc0112` | `e70841fc0112` |
| checksum: payments | pass | `13d63cd5af74` | `13d63cd5af74` |
| no orphans: orders.customer_id | pass | `0` | `0` |
| no orphans: order_items.order_id | pass | `0` | `0` |
| no orphans: payments.order_id | pass | `0` | `0` |
| business query: total paid revenue | pass | `164470639.48` | `164470639.48` |
| paid orders have a payment | pass | `0` | `0` |

## What the numbers mean

- **RTO** (recovery time): from starting the restore to having verified, usable data.
- **RPO** (data loss window): how old the restored data was when the drill started - everything written after the restore point would be lost in a real incident.
