"""Data checks run against a restored database."""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .dataset import FOREIGN_KEYS, TABLES, Manifest, table_checksum


@dataclass
class CheckResult:
    name: str
    ok: bool
    expected: str
    actual: str

    def to_dict(self) -> dict[str, str | bool]:
        return {"name": self.name, "ok": self.ok, "expected": self.expected, "actual": self.actual}


def verify(conninfo: str, manifest: Manifest) -> list[CheckResult]:
    out: list[CheckResult] = []
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        # Row text (and so the checksum) depends on session settings: pin them on both sides.
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("SET datestyle TO 'ISO, YMD'")
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public.customers",))
        if not cur.fetchone()[0]:
            return [CheckResult("schema present", False, "tables customers/orders/order_items/payments", "missing")]
        for t in TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            n = cur.fetchone()[0]
            exp = manifest.row_counts.get(t)
            out.append(CheckResult(f"row count: {t}", n == exp, str(exp), str(n)))
        for t in TABLES:
            got = table_checksum(cur, t)
            exp = manifest.checksums.get(t, "")
            out.append(CheckResult(f"checksum: {t}", got == exp, exp[:12], got[:12]))
        for child, col, parent in FOREIGN_KEYS:
            cur.execute(f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p ON p.id = c.{col} WHERE p.id IS NULL")
            orphans = cur.fetchone()[0]
            out.append(CheckResult(f"no orphans: {child}.{col}", orphans == 0, "0", str(orphans)))
        cur.execute("SELECT coalesce(sum(amount), 0)::text FROM payments")
        rev = cur.fetchone()[0]
        out.append(CheckResult("business query: total paid revenue", rev == manifest.revenue_paid, manifest.revenue_paid, rev))
        cur.execute("SELECT count(*) FROM orders o WHERE o.status IN ('paid','shipped','delivered') "
                    "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)")
        unpaid = cur.fetchone()[0]
        out.append(CheckResult("paid orders have a payment", unpaid == 0, "0", str(unpaid)))
    return out
