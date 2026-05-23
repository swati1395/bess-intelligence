"""
Dashboard generator — reads data/intel.db, writes docs/index.html.

GitHub Pages serves the docs/ folder; committing an updated index.html refreshes
the published dashboard. Self-contained: inline CSS, no external assets.

Run: python3 dashboard/build.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from html import escape

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from database.setup_db import DB_PATH, init_db  # noqa: E402

OUTPUT_PATH = os.path.join(PROJECT_ROOT, "docs", "index.html")


def fmt_num(value, suffix="") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{int(value):,}{suffix}"


def fetch_summary(cur: sqlite3.Cursor) -> dict:
    summary = {}
    cur.execute("SELECT COUNT(*) FROM articles")
    summary["articles"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM extracted_intel")
    summary["extracted"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM extracted_intel WHERE significance_score >= 4")
    summary["high_sig"] = cur.fetchone()[0]
    cur.execute(
        """SELECT ROUND(SUM(storage_mw)/1000.0, 2) FROM pipeline_projects
           WHERE q_status IN ('active', 'operational')"""
    )
    summary["pipeline_gw"] = cur.fetchone()[0] or 0
    return summary


def fetch_recent_high_sig(cur: sqlite3.Cursor, limit: int = 15) -> list:
    cur.execute(
        """
        SELECT a.title, a.url, a.source_name, ei.company, ei.event_type,
               ei.iso_market, ei.capacity_mwh, ei.significance_score,
               ei.significance_reason, ei.extracted_at
        FROM extracted_intel ei
        JOIN articles a ON a.id = ei.article_id
        WHERE ei.significance_score >= 3
        ORDER BY ei.significance_score DESC, ei.extracted_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    keys = ("title", "url", "source", "company", "event_type", "iso_market",
            "capacity_mwh", "score", "reason", "extracted_at")
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def fetch_pipeline_by_iso(cur: sqlite3.Cursor) -> list:
    cur.execute(
        """
        SELECT iso_market, q_status, COUNT(*),
               ROUND(SUM(COALESCE(storage_mw, 0))/1000.0, 2)
        FROM pipeline_projects
        GROUP BY iso_market, q_status
        ORDER BY iso_market, 4 DESC
        """
    )
    return [{"iso": r[0], "status": r[1], "count": r[2], "gw": r[3] or 0}
            for r in cur.fetchall()]


def fetch_top_developers(cur: sqlite3.Cursor, limit: int = 10) -> list:
    cur.execute(
        """
        SELECT developer, COUNT(*),
               ROUND(SUM(COALESCE(storage_mw, 0))/1000.0, 3),
               SUM(CASE WHEN iso_market='ERCOT' THEN 1 ELSE 0 END),
               SUM(CASE WHEN iso_market='CAISO' THEN 1 ELSE 0 END)
        FROM pipeline_projects
        WHERE developer IS NOT NULL AND storage_mw IS NOT NULL AND storage_mw > 0
        GROUP BY developer
        ORDER BY 3 DESC
        LIMIT ?
        """,
        (limit,),
    )
    keys = ("name", "n_projects", "gw", "n_ercot", "n_caiso")
    return [dict(zip(keys, r)) for r in cur.fetchall()]


def render_article_card(a: dict) -> str:
    score = a["score"]
    score_class = f"score-{score}"
    capacity = f"{a['capacity_mwh']:g} MWh" if a["capacity_mwh"] else "—"
    return f"""
    <article class="card article {score_class}">
      <div class="article-head">
        <span class="badge badge-{score}">{score}/5</span>
        <a class="article-title" href="{escape(a['url'] or '#', quote=True)}" target="_blank" rel="noopener">{escape(a['title'] or '(untitled)')}</a>
      </div>
      <div class="article-meta">
        <span>{escape(a['source'] or '')}</span> ·
        <span>{escape(a['company'] or 'No company')}</span> ·
        <span>{escape(a['event_type'] or '—')}</span> ·
        <span>{escape(a['iso_market'] or 'No ISO')}</span> ·
        <span>{escape(capacity)}</span>
      </div>
      <p class="article-reason">{escape(a['reason'] or '')}</p>
    </article>
    """


def render_pipeline_section(rows: list) -> str:
    by_iso = {}
    for r in rows:
        by_iso.setdefault(r["iso"], []).append(r)
    blocks = []
    for iso in ("ERCOT", "CAISO"):
        if iso not in by_iso:
            continue
        total_count = sum(r["count"] for r in by_iso[iso])
        total_gw = sum(r["gw"] for r in by_iso[iso])
        rows_html = "\n".join(
            f"<tr><td>{escape(r['status'] or '?')}</td>"
            f"<td class='num'>{fmt_num(r['count'])}</td>"
            f"<td class='num'>{fmt_num(r['gw'], ' GW')}</td></tr>"
            for r in by_iso[iso]
        )
        blocks.append(f"""
        <div class="card pipeline-iso">
          <h3>{iso}</h3>
          <table>
            <thead><tr><th>Status</th><th class='num'>Projects</th><th class='num'>Capacity</th></tr></thead>
            <tbody>{rows_html}</tbody>
            <tfoot><tr><th>Total</th>
              <th class='num'>{fmt_num(total_count)}</th>
              <th class='num'>{fmt_num(total_gw, ' GW')}</th></tr></tfoot>
          </table>
        </div>
        """)
    return "".join(blocks)


def render_developers_table(devs: list) -> str:
    if not devs:
        return "<p class='empty'>No named developers with capacity in the queue.</p>"
    rows = "\n".join(
        f"<tr><td>{i}</td><td>{escape(d['name'])}</td>"
        f"<td class='num'>{fmt_num(d['n_projects'])}</td>"
        f"<td class='num'>{fmt_num(d['gw'], ' GW')}</td>"
        f"<td class='num'>{d['n_ercot']}/{d['n_caiso']}</td></tr>"
        for i, d in enumerate(devs, 1)
    )
    return f"""
    <div class="card">
      <table class="developers">
        <thead><tr><th>#</th><th>Developer</th><th class='num'>Projects</th><th class='num'>GW</th><th class='num'>ERCOT / CAISO</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


CSS = """
:root {
  --bg: #f8fafc;
  --card: #ffffff;
  --border: #e2e8f0;
  --text: #0f172a;
  --muted: #64748b;
  --accent: #2563eb;
  --warning: #ea580c;
  --critical: #dc2626;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
}
.container { max-width: 1100px; margin: 0 auto; padding: 32px 24px 64px; }

header { margin-bottom: 32px; padding-bottom: 16px; border-bottom: 2px solid var(--text); }
header h1 { margin: 0; font-size: 24px; letter-spacing: -0.01em; }
header .tagline { color: var(--muted); font-size: 14px; margin-top: 6px; }
header .updated { color: var(--muted); font-size: 12px; margin-top: 8px; font-family: ui-monospace, "SF Mono", Menlo, monospace; }

h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--muted); margin: 36px 0 12px; font-weight: 600; }

.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 16px 20px; margin-bottom: 12px; }

.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 16px 18px; }
.stat .label { font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.06em; }
.stat .value { font-size: 28px; font-weight: 700; margin-top: 4px; letter-spacing: -0.02em; }
.stat .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }

.article { border-left: 3px solid var(--accent); }
.article.score-3 { border-left-color: #ca8a04; }
.article.score-4 { border-left-color: var(--warning); background: #fffbeb; }
.article.score-5 { border-left-color: var(--critical); background: #fef2f2; }
.article-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }
.article-title { font-weight: 600; font-size: 15px; color: var(--text); text-decoration: none; line-height: 1.35; }
.article-title:hover { text-decoration: underline; }
.article-meta { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.article-reason { margin: 0; font-size: 13px; color: var(--muted); font-style: italic; }

.badge { display: inline-block; padding: 1px 7px; border-radius: 4px;
         font-size: 10px; font-weight: 700; color: white; vertical-align: middle; }
.badge-3 { background: #ca8a04; }
.badge-4 { background: var(--warning); }
.badge-5 { background: var(--critical); }

.pipeline-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.pipeline-iso h3 { margin: 0 0 12px; font-size: 14px; font-weight: 700; letter-spacing: -0.01em; }

table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.04em; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
tfoot th { border-bottom: none; border-top: 2px solid var(--text); color: var(--text); }

.developers tbody tr td:nth-child(1) { color: var(--muted); width: 28px; }
.developers tbody tr td:nth-child(2) { font-weight: 500; }

footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
         font-size: 11px; color: var(--muted); text-align: center; }
footer code { font-family: ui-monospace, Menlo, monospace; }

@media (max-width: 720px) {
  .stats { grid-template-columns: 1fr 1fr; }
  .pipeline-grid { grid-template-columns: 1fr; }
  header h1 { font-size: 20px; }
}
"""


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BESS Market Intelligence Dashboard</title>
<style>{css}</style>
</head>
<body>
<div class="container">

  <header>
    <h1>BESS Market Intelligence</h1>
    <div class="tagline">ERCOT &amp; CAISO competitive intelligence — battery energy storage systems</div>
    <div class="updated">Last refreshed: {updated_at}</div>
  </header>

  <h2>At a glance</h2>
  <section class="stats">
    <div class="stat"><div class="label">Articles tracked</div>
      <div class="value">{articles}</div>
      <div class="sub">across 7 RSS sources</div></div>
    <div class="stat"><div class="label">Structured extractions</div>
      <div class="value">{extracted}</div>
      <div class="sub">via Claude Opus 4.7</div></div>
    <div class="stat"><div class="label">High-significance (4+)</div>
      <div class="value">{high_sig}</div>
      <div class="sub">market-relevant signals</div></div>
    <div class="stat"><div class="label">Active pipeline</div>
      <div class="value">{pipeline_gw}</div>
      <div class="sub">GW — ERCOT + CAISO, active &amp; operational</div></div>
  </section>

  <h2>Recent high-significance articles</h2>
  <section>{articles_html}</section>

  <h2>Interconnection queue — by ISO &amp; status</h2>
  <section class="pipeline-grid">{pipeline_html}</section>
  <p style="font-size:11px; color: var(--muted); margin: 8px 0 0 4px;">
    Source: LBNL Queued Up 2026. GW = nameplate power capacity (no duration in dataset).</p>

  <h2>Top 10 named developers by queue capacity</h2>
  {developers_html}

  <footer>
    Generated from <code>data/intel.db</code> by <code>dashboard/build.py</code> ·
    Updated via GitHub Actions ·
    <a href="https://github.com/swati1395/bess-intelligence">source</a>
  </footer>
</div>
</body>
</html>
"""


def build() -> None:
    conn = init_db()
    cur = conn.cursor()

    summary = fetch_summary(cur)
    articles = fetch_recent_high_sig(cur)
    pipeline = fetch_pipeline_by_iso(cur)
    developers = fetch_top_developers(cur)

    articles_html = "".join(render_article_card(a) for a in articles) \
        if articles else "<p class='empty'>No high-significance articles yet.</p>"

    html_doc = HTML_TEMPLATE.format(
        css=CSS,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        articles=fmt_num(summary["articles"]),
        extracted=fmt_num(summary["extracted"]),
        high_sig=fmt_num(summary["high_sig"]),
        pipeline_gw=fmt_num(summary["pipeline_gw"]),
        articles_html=articles_html,
        pipeline_html=render_pipeline_section(pipeline),
        developers_html=render_developers_table(developers),
    )

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html_doc)

    print(f"Wrote {OUTPUT_PATH}  ({len(html_doc):,} bytes)")
    print(f"  Articles: {summary['articles']:,}  •  Extracted: {summary['extracted']:,}  •  "
          f"High-sig: {summary['high_sig']}  •  Pipeline: {summary['pipeline_gw']} GW")

    conn.close()


if __name__ == "__main__":
    build()
