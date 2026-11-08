"""Drill report as Markdown, HTML and JSON."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .drill import DrillResult
from .isolation import IsolationFinding


def human(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 172800:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _verdict(ok: bool | None) -> str:
    return {True: "met", False: "MISSED", None: "no target set"}[ok]


def to_markdown(r: DrillResult, iso: list[IsolationFinding] | None = None) -> str:
    lines = [f"# Restore drill report - {'PASSED' if r.passed else 'FAILED'}", ""]
    if r.synthetic:
        lines += ["> Synthetic demo data (generated customers/orders). No real customer data.", ""]
    lines += [
        f"- Drill type: **{r.kind}**, started {r.started_at}",
        f"- Restored from: {r.source} (data as of {r.restore_point})",
        f"- Restored into: `{r.target}` (deleted after the drill unless kept)",
        f"- **RTO achieved: {human(r.rto_seconds)}** (target: {human(r.rto_target_seconds) if r.rto_target_seconds else '-'}, {_verdict(r.rto_ok)})",
        f"- **RPO at drill time: {human(r.rpo_seconds)}** (target: {human(r.rpo_target_seconds) if r.rpo_target_seconds else '-'}, {_verdict(r.rpo_ok)})",
        f"- Data checks: {sum(c.ok for c in r.checks)}/{len(r.checks)} passed",
        "",
        "## Timeline",
        "",
        "| Phase | Duration |",
        "|---|---|",
    ]
    lines += [f"| {k} | {human(v)} |" for k, v in r.phases.items()]
    lines += ["", "## Data checks", "", "| Check | Result | Expected | Actual |", "|---|---|---|---|"]
    lines += [f"| {c.name} | {'pass' if c.ok else '**FAIL**'} | `{c.expected}` | `{c.actual}` |" for c in r.checks]
    if iso:
        lines += ["", "## Backup isolation", "", "| Check | Status | Detail | Fix |", "|---|---|---|---|"]
        lines += [f"| {f.check} | {f.status} | {f.detail} | {f.fix} |" for f in iso]
    lines += ["", "## What the numbers mean", "",
              "- **RTO** (recovery time): from starting the restore to having verified, usable data.",
              "- **RPO** (data loss window): how old the restored data was when the drill started - "
              "everything written after the restore point would be lost in a real incident."]
    if r.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in r.notes]
    return "\n".join(lines) + "\n"


CSS = """
:root{--bg:#f5f6f8;--fg:#18202e;--card:#fff;--line:#dfe3e8;--muted:#5b6475;--ok:#1a7f37;--bad:#cf222e;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--fg:#e8edf3;--card:#161b22;--line:#2d333b;--muted:#94a0b0;--ok:#57ab5a;--bad:#f47067;--warn:#d29922}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:28px 16px 48px}h1{margin:0;font-size:26px}h2{font-size:19px;margin-top:28px}
.muted{color:var(--muted)}.banner{background:#fff4ce;color:#5c4400;border:1px solid #e3c35a;border-radius:8px;padding:8px 12px;margin:12px 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.v{font-size:28px;font-weight:700}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);font-size:14px}
td,th{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;color:var(--muted);text-transform:uppercase}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}.warn{color:var(--warn);font-weight:600}
.bar{height:10px;background:var(--ok);border-radius:4px}
"""


def to_html(r: DrillResult, iso: list[IsolationFinding] | None = None) -> str:
    e = html.escape
    status = '<span class="ok">PASSED</span>' if r.passed else '<span class="bad">FAILED</span>'
    total = sum(r.phases.values()) or 1
    rows = "".join(
        f"<tr><td>{e(k)}</td><td>{human(v)}</td><td style='width:45%'><div class='bar' style='width:{max(2, int(v / total * 100))}%'></div></td></tr>"
        for k, v in r.phases.items())
    checks = "".join(
        f"<tr><td>{e(c.name)}</td><td class='{'ok' if c.ok else 'bad'}'>{'pass' if c.ok else 'FAIL'}</td>"
        f"<td><code>{e(c.expected)}</code></td><td><code>{e(c.actual)}</code></td></tr>" for c in r.checks)
    iso_html = ""
    if iso:
        cls = {"pass": "ok", "fail": "bad", "warn": "warn", "info": "muted"}
        iso_rows = "".join(f"<tr><td>{e(f.check)}</td><td class='{cls.get(f.status, '')}'>{e(f.status)}</td><td>{e(f.detail)}</td><td>{e(f.fix)}</td></tr>" for f in iso)
        iso_html = f"<h2>Backup isolation</h2><table><tr><th>Check</th><th>Status</th><th>Detail</th><th>Fix</th></tr>{iso_rows}</table>"
    rto_t = human(r.rto_target_seconds) if r.rto_target_seconds else "no target"
    rpo_t = human(r.rpo_target_seconds) if r.rpo_target_seconds else "no target"
    banner = "<div class='banner'>Synthetic demo data (generated customers/orders). No real customer data.</div>" if r.synthetic else ""
    notes = "".join(f"<li>{e(n)}</li>" for n in r.notes)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Restore drill report</title><style>{CSS}</style></head><body><main>
<h1>Restore drill: {status}</h1>
<div class="muted">{e(r.kind)} &middot; started {e(r.started_at)} &middot; from {e(r.source)}</div>{banner}
<div class="cards">
<div class="card"><div class="muted">RTO achieved</div><div class="v">{human(r.rto_seconds)}</div><div class="muted">target {rto_t} &middot; {_verdict(r.rto_ok)}</div></div>
<div class="card"><div class="muted">RPO at drill time</div><div class="v">{human(r.rpo_seconds)}</div><div class="muted">target {rpo_t} &middot; {_verdict(r.rpo_ok)}</div></div>
<div class="card"><div class="muted">Data checks</div><div class="v">{sum(c.ok for c in r.checks)}/{len(r.checks)}</div><div class="muted">restore point {e(r.restore_point)}</div></div>
</div>
<h2>Timeline</h2><table><tr><th>Phase</th><th>Duration</th><th></th></tr>{rows}</table>
<h2>Data checks</h2><table><tr><th>Check</th><th>Result</th><th>Expected</th><th>Actual</th></tr>{checks}</table>
{iso_html}
<h2>What the numbers mean</h2><ul><li><b>RTO</b>: from starting the restore to verified, usable data.</li>
<li><b>RPO</b>: how old the restored data was when the drill started; writes after the restore point would be lost.</li>{notes}</ul>
<p class="muted">Generated by restore-drill-kit &middot; github.com/ObaidAbdullah16/restore-drill-kit</p>
</main></body></html>
"""


def write_all(r: DrillResult, out_dir: str, iso: list[IsolationFinding] | None = None) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = r.to_dict()
    if iso:
        data["isolation"] = [f.to_dict() for f in iso]
    files = {"report.md": to_markdown(r, iso), "report.html": to_html(r, iso), "drill.json": json.dumps(data, indent=2)}
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
    return [out / n for n in files]
