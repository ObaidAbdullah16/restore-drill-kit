"""Synthetic shop database (customers, orders, order_items, payments) and the
"manifest": row counts, checksums and freshness captured at backup time, so a
restore can be *proven* identical instead of eyeballed."""
from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          bigint PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    city        text NOT NULL,
    created_at  timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id          bigint PRIMARY KEY,
    customer_id bigint NOT NULL REFERENCES customers(id),
    status      text NOT NULL CHECK (status IN ('placed','paid','shipped','delivered','cancelled')),
    created_at  timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    id          bigint PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    sku         text NOT NULL,
    quantity    int NOT NULL CHECK (quantity > 0),
    unit_price  numeric(10,2) NOT NULL CHECK (unit_price >= 0)
);
CREATE TABLE IF NOT EXISTS payments (
    id          bigint PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id),
    amount      numeric(12,2) NOT NULL,
    paid_at     timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders(customer_id);
CREATE INDEX IF NOT EXISTS items_order_idx ON order_items(order_id);
CREATE INDEX IF NOT EXISTS payments_order_idx ON payments(order_id);
"""

TABLES = ["customers", "orders", "order_items", "payments"]
FRESHNESS = {"customers": "created_at", "orders": "created_at", "payments": "paid_at"}
FOREIGN_KEYS = [  # (child table, column, parent table)
    ("orders", "customer_id", "customers"),
    ("order_items", "order_id", "orders"),
    ("payments", "order_id", "orders"),
]
CITIES = ["Indore", "Pune", "Bengaluru", "Hyderabad", "Delhi", "Mumbai", "Chennai", "Jaipur", "Kochi", "Ahmedabad"]
FIRST = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Sara", "Arjun", "Isha", "Vihaan", "Anaya", "Zoya", "Dev"]
LAST = ["Sharma", "Khan", "Patel", "Iyer", "Singh", "Das", "Mehta", "Reddy", "Joshi", "Ali"]
SKUS = [f"SKU-{n:04d}" for n in range(1, 61)]


def generate(customers: int = 500, seed: int = 42, now: dt.datetime | None = None) -> dict[str, list[tuple]]:
    """Deterministic synthetic rows. Same seed + now = same data."""
    rnd = random.Random(seed)
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)
    start = now - dt.timedelta(days=365)
    rows: dict[str, list[tuple]] = {t: [] for t in TABLES}
    oid = iid = pid = 0
    for cid in range(1, customers + 1):
        created = start + dt.timedelta(seconds=rnd.randint(0, 300 * 86400))
        name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        email = f"{name.lower().replace(' ', '.')}.{cid}@example.test"
        rows["customers"].append((cid, email, name, rnd.choice(CITIES), created))
        for _ in range(rnd.randint(0, 6)):
            oid += 1
            ocreated = created + dt.timedelta(seconds=rnd.randint(60, max(61, int((now - created).total_seconds()) - 60)))
            status = rnd.choices(["placed", "paid", "shipped", "delivered", "cancelled"], [1, 2, 3, 8, 1])[0]
            rows["orders"].append((oid, cid, status, ocreated))
            total = 0.0
            for _ in range(rnd.randint(1, 4)):
                iid += 1
                qty, price = rnd.randint(1, 3), round(rnd.uniform(99, 4999), 2)
                total += qty * price
                rows["order_items"].append((iid, oid, rnd.choice(SKUS), qty, price))
            if status != "placed" and status != "cancelled":
                pid += 1
                rows["payments"].append((pid, oid, round(total, 2), ocreated + dt.timedelta(minutes=rnd.randint(1, 120))))
    return rows


def load(conninfo: str, customers: int = 500, seed: int = 42, now: dt.datetime | None = None) -> dict[str, int]:
    """Create the schema and insert the synthetic rows (replacing any previous demo data)."""
    rows = generate(customers, seed, now)
    with psycopg.connect(conninfo, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute("TRUNCATE payments, order_items, orders, customers")
        for table in TABLES:
            if not rows[table]:
                continue
            with cur.copy(f"COPY {table} FROM STDIN") as cp:
                for r in rows[table]:
                    cp.write_row(r)
        conn.commit()
    return {t: len(v) for t, v in rows.items()}


@dataclass
class Manifest:
    """What the backup is supposed to contain, captured from the source at backup time."""
    captured_at: str
    source: str
    row_counts: dict[str, int] = field(default_factory=dict)
    checksums: dict[str, str] = field(default_factory=dict)
    latest: dict[str, str | None] = field(default_factory=dict)
    revenue_paid: str = "0"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        return cls(**json.loads(text))


def table_checksum(cur: Any, table: str) -> str:
    # Row text depends on the session time zone and date style, so callers pin both first.
    cur.execute(f"SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t.id)), 'empty') FROM {table} t")
    return cur.fetchone()[0]


def capture(conninfo: str, source_label: str = "") -> Manifest:
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        # Row text (and so the checksum) depends on session settings: pin them on both sides.
        cur.execute("SET TIME ZONE 'UTC'")
        cur.execute("SET datestyle TO 'ISO, YMD'")
        m = Manifest(captured_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), source=source_label)
        for t in TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            m.row_counts[t] = cur.fetchone()[0]
            m.checksums[t] = table_checksum(cur, t)
        for t, col in FRESHNESS.items():
            cur.execute(f"SELECT max({col}) FROM {t}")
            v = cur.fetchone()[0]
            m.latest[t] = v.isoformat() if v else None
        cur.execute("SELECT coalesce(sum(amount), 0)::text FROM payments")
        m.revenue_paid = cur.fetchone()[0]
    return m
