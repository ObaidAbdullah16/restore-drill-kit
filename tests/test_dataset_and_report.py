import datetime as dt

from restoredrill.cli import duration
from restoredrill.dataset import generate
from restoredrill.drill import DrillResult
from restoredrill.report import human, to_html, to_markdown
from restoredrill.verify import CheckResult

NOW = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)


def test_generate_is_deterministic_and_consistent():
    a, b = generate(100, seed=7, now=NOW), generate(100, seed=7, now=NOW)
    assert a == b
    order_ids = {o[0] for o in a["orders"]}
    assert all(i[1] in order_ids for i in a["order_items"])
    assert all(p[1] in order_ids for p in a["payments"])
    assert all(o[3] <= NOW for o in a["orders"])
    paid = {o[0] for o in a["orders"] if o[2] in ("paid", "shipped", "delivered")}
    assert paid == {p[1] for p in a["payments"]}


def test_human_and_duration():
    assert human(0.42) == "0.4s"
    assert human(75) == "1m 15s"
    assert human(86400) == "24h 00m"
    assert human(3 * 86400 + 7200) == "3d 2h"
    assert duration("1h30m") == 5400 and duration("45s") == 45 and duration("2d") == 172800


def _result(ok=True, rto=120.0, target=None):
    r = DrillResult(kind="local", source="src", restore_point="2026-10-24T00:00:00+00:00",
                    started_at="2026-10-24T01:00:00+00:00", rto_seconds=rto, rpo_seconds=3600,
                    rto_target_seconds=target)
    r.phases = {"restore": rto - 10, "verify data": 10}
    r.checks = [CheckResult("row count: x", ok, "1", "1" if ok else "0")]
    return r


def test_pass_fail_logic():
    assert _result().passed
    assert not _result(ok=False).passed
    assert not _result(rto=600, target=300).passed
    assert _result(rto=200, target=300).rto_ok is True


def test_reports_render_and_escape():
    r = _result(ok=False)
    r.checks[0].actual = "<script>"
    md, page = to_markdown(r), to_html(r)
    assert "FAILED" in md and "**FAIL**" in md
    assert "&lt;script&gt;" in page and "<script>" not in page
