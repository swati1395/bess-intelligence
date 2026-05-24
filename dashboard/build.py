"""
Dashboard generator — reads data/intel.db, writes docs/index.html.

Produces a self-contained interactive HTML page (inline CSS + vanilla JS, no
external assets). Client-side filtering on score, ISO market, company, event
type, and a real-time title keyword search.

Run: python3 dashboard/build.py
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from html import escape

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from database.setup_db import DB_PATH, init_db  # noqa: E402

OUTPUT_PATH = os.path.join(PROJECT_ROOT, "docs", "index.html")
NO_MARKET_SENTINEL = "(none)"

CATEGORY_SLUGS = {
    "Direct BESS":         "direct",
    "Supply Chain":        "supply",
    "Adjacent Market":     "adjacent",
    "Policy & Regulation": "policy",
    "Market Structure":    "market",
    "M&A":                 "mna",
}


def fmt_num(value, suffix="") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}{suffix}"
    return f"{int(value):,}{suffix}"


def label_from_snake(s: str) -> str:
    return s.replace("_", " ").capitalize() if s else "—"


def short_date(value) -> str:
    if not value:
        return ""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value))
    return m.group(1) if m else str(value)[:10]


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


def fetch_articles(cur: sqlite3.Cursor) -> list:
    cur.execute(
        """
        SELECT a.title, a.url, a.source_name,
               COALESCE(NULLIF(a.published_at, ''), a.published_date, '') AS published,
               ei.company, ei.event_type, ei.iso_market, ei.capacity_mwh,
               ei.significance_score, ei.significance_reason, ei.category
        FROM extracted_intel ei
        JOIN articles a ON a.id = ei.article_id
        WHERE ei.significance_score >= 3
        ORDER BY ei.significance_score DESC,
                 COALESCE(NULLIF(a.published_at, ''), a.published_date) DESC
        """
    )
    keys = ("title", "url", "source", "published", "company", "event_type",
            "iso_market", "capacity_mwh", "score", "reason", "category")
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
    capacity = f"{a['capacity_mwh']:g} MWh" if a["capacity_mwh"] else "—"
    iso = a["iso_market"] or NO_MARKET_SENTINEL
    company = a["company"] or NO_MARKET_SENTINEL
    event = a["event_type"] or NO_MARKET_SENTINEL
    category = a.get("category") or NO_MARKET_SENTINEL
    title = a["title"] or "(untitled)"
    published = short_date(a["published"])
    cat_html = ""
    if category and category in CATEGORY_SLUGS:
        cat_html = f'<span class="cat-badge cat-{CATEGORY_SLUGS[category]}">{escape(category)}</span>'
    return f"""
    <article class="article-card score-{score}"
             data-score="{score}"
             data-iso="{escape(iso, quote=True)}"
             data-company="{escape(company, quote=True)}"
             data-event="{escape(event, quote=True)}"
             data-category="{escape(category, quote=True)}"
             data-title="{escape(title.lower(), quote=True)}">
      <div class="article-head">
        <span class="badge badge-{score}">{score}/5</span>{cat_html}
        <a class="article-title" href="{escape(a['url'] or '#', quote=True)}" target="_blank" rel="noopener">{escape(title)}</a>
      </div>
      <div class="article-meta">
        <span>{escape(a['source'] or '')}</span>
        {f"<span>· {escape(published)}</span>" if published else ""}
        <span>· {escape(a['company'] or 'No company')}</span>
        <span>· {escape(label_from_snake(a['event_type'] or ''))}</span>
        <span>· {escape(a['iso_market'] or 'No specific market')}</span>
        <span>· {escape(capacity)}</span>
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


def render_filter_options(articles: list) -> dict:
    # Build dropdown option lists from observed data
    isos_in_data = sorted({a["iso_market"] for a in articles if a["iso_market"]})
    # Spec asks for AEMO and PJM specifically; include them even if absent
    spec_isos = ["ERCOT", "CAISO", "AEMO", "PJM"]
    iso_options = []
    for v in spec_isos:
        iso_options.append((v, v))
    for v in isos_in_data:
        if v not in spec_isos:
            iso_options.append((v, v))
    iso_options.append((NO_MARKET_SENTINEL, "No specific market"))

    companies = sorted({a["company"] for a in articles if a["company"]},
                       key=lambda s: s.lower())
    company_options = [(c, c) for c in companies]
    company_options.append((NO_MARKET_SENTINEL, "(No company identified)"))

    events_in_data = sorted({a["event_type"] for a in articles if a["event_type"]})
    event_options = [(v, label_from_snake(v)) for v in events_in_data]
    event_options.append((NO_MARKET_SENTINEL, "(No event type)"))

    # Categories: show all 6 canonical labels even if some don't appear yet in data
    canonical_cats = list(CATEGORY_SLUGS.keys())
    cats_in_data = {a.get("category") for a in articles if a.get("category")}
    category_options = [(c, c) for c in canonical_cats]
    for c in sorted(cats_in_data - set(canonical_cats)):
        category_options.append((c, c))
    category_options.append((NO_MARKET_SENTINEL, "(Uncategorized)"))

    return {"iso": iso_options, "company": company_options,
            "event": event_options, "category": category_options}


def render_dropdown(name: str, options: list, all_label: str) -> str:
    opts = [f'<option value="ALL">{escape(all_label)}</option>']
    for value, label in options:
        opts.append(f'<option value="{escape(value, quote=True)}">{escape(label)}</option>')
    return f'<select id="filter-{name}">{"".join(opts)}</select>'


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
  --gold: #ca8a04;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--text); line-height: 1.5;
}
.container { max-width: 1100px; margin: 0 auto; padding: 28px 24px 64px; }

header { margin-bottom: 24px; padding-bottom: 16px; border-bottom: 2px solid var(--text); }
header h1 { margin: 0; font-size: 24px; letter-spacing: -0.01em; }
header .tagline { color: var(--muted); font-size: 14px; margin-top: 6px; }
header .updated { color: var(--muted); font-size: 12px; margin-top: 8px;
                  font-family: ui-monospace, "SF Mono", Menlo, monospace; }

.coverage-note { background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px;
                 padding: 12px 16px; font-size: 13px; color: #78350f; margin-bottom: 24px; }
.coverage-note strong { color: #92400e; }

h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--muted); margin: 32px 0 12px; font-weight: 600; }

.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 16px 20px; margin-bottom: 12px; }

.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 16px 18px; }
.stat .label { font-size: 11px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.06em; }
.stat .value { font-size: 28px; font-weight: 700; margin-top: 4px; letter-spacing: -0.02em; }
.stat .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }

.legend { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
          padding: 14px 18px; margin-bottom: 16px; }
.legend summary { cursor: pointer; font-weight: 600; font-size: 13px; color: var(--text);
                  list-style: none; display: flex; align-items: center; gap: 8px; }
.legend summary::after { content: "▸"; color: var(--muted); margin-left: auto; transition: transform 0.15s; }
.legend[open] summary::after { transform: rotate(90deg); }
.legend .scale { margin-top: 12px; display: grid; gap: 6px; }
.legend .row { display: grid; grid-template-columns: 28px 1fr; align-items: start;
               font-size: 13px; line-height: 1.5; }
.legend .row .b { display: inline-block; width: 24px; text-align: center; padding: 1px 0;
                  border-radius: 4px; font-size: 10px; font-weight: 700; color: white; }

.filters { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
           padding: 14px 16px; margin-bottom: 12px;
           display: grid; gap: 12px;
           grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.filter-group { display: flex; flex-direction: column; gap: 4px; }
.filter-group label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
                      color: var(--muted); font-weight: 600; }
.filter-group select, .filter-group input {
  font: inherit; font-size: 13px;
  padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text); width: 100%; }
.filter-group select:focus, .filter-group input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }

.filter-actions { display: flex; align-items: end; gap: 8px; }
button.reset-btn { font: inherit; font-size: 12px; padding: 7px 12px;
                   border: 1px solid var(--border); border-radius: 6px;
                   background: var(--bg); color: var(--text); cursor: pointer; }
button.reset-btn:hover { background: var(--border); }

.count-bar { font-size: 13px; color: var(--muted); margin: 4px 0 12px; font-variant-numeric: tabular-nums; }
.count-bar strong { color: var(--text); }

.article-card { background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--accent);
                border-radius: 8px; padding: 14px 18px; margin-bottom: 10px; }
.article-card.score-3 { border-left-color: var(--gold); }
.article-card.score-4 { border-left-color: var(--warning); background: #fffbeb; }
.article-card.score-5 { border-left-color: var(--critical); background: #fef2f2; }
.article-card.hidden { display: none; }
.article-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; flex-wrap: wrap; }
.article-title { font-weight: 600; font-size: 15px; color: var(--text); text-decoration: none; line-height: 1.35; }
.article-title:hover { text-decoration: underline; }
.article-meta { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
.article-meta span { margin-right: 2px; }
.article-reason { margin: 0; font-size: 13px; color: var(--muted); font-style: italic; }

.empty-state { padding: 32px; text-align: center; color: var(--muted); font-size: 14px;
               border: 1px dashed var(--border); border-radius: 8px; }

.badge { display: inline-block; padding: 1px 7px; border-radius: 4px;
         font-size: 10px; font-weight: 700; color: white; vertical-align: middle; }
.badge-3 { background: var(--gold); }
.badge-4 { background: var(--warning); }
.badge-5 { background: var(--critical); }

.cat-badge { display: inline-block; padding: 1px 7px; border-radius: 4px;
             font-size: 10px; font-weight: 600; margin-left: 6px;
             border: 1px solid currentColor; vertical-align: middle;
             white-space: nowrap; }
.cat-direct   { color: #1e40af; background: #dbeafe; border-color: #93c5fd; }
.cat-supply   { color: #5b21b6; background: #ede9fe; border-color: #c4b5fd; }
.cat-adjacent { color: #4d7c0f; background: #ecfccb; border-color: #bef264; }
.cat-policy   { color: #9a3412; background: #ffedd5; border-color: #fdba74; }
.cat-market   { color: #075985; background: #e0f2fe; border-color: #7dd3fc; }
.cat-mna      { color: #831843; background: #fce7f3; border-color: #f9a8d4; }

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
  .filter-actions { justify-content: stretch; }
  button.reset-btn { flex: 1; }
}
"""


# JavaScript embedded as a single string. {} are not used for f-string interpolation
# inside JS — we keep this raw and concatenate with HTML below.
JS = """
(function () {
  const sigEl      = document.getElementById('filter-sig');
  const isoEl      = document.getElementById('filter-iso');
  const companyEl  = document.getElementById('filter-company');
  const eventEl    = document.getElementById('filter-event');
  const categoryEl = document.getElementById('filter-category');
  const qEl        = document.getElementById('filter-q');
  const resetBtn   = document.getElementById('reset-filters');
  const cards     = Array.from(document.querySelectorAll('.article-card'));
  const visEl     = document.getElementById('count-visible');
  const totalEl   = document.getElementById('count-total');
  const emptyEl   = document.getElementById('empty-state');
  const TOTAL     = cards.length;
  totalEl.textContent = TOTAL;

  function passSig(card, val) {
    const s = parseInt(card.dataset.score, 10);
    if (val === 'high') return s >= 4;
    if (val === 'top')  return s >= 5;
    return s >= 3; // 'all'
  }

  function apply() {
    const sig      = sigEl.value;
    const iso      = isoEl.value;
    const company  = companyEl.value;
    const event    = eventEl.value;
    const category = categoryEl.value;
    const q        = qEl.value.trim().toLowerCase();

    let visible = 0;
    for (const card of cards) {
      let pass = passSig(card, sig);
      if (pass && iso      !== 'ALL' && card.dataset.iso      !== iso)      pass = false;
      if (pass && company  !== 'ALL' && card.dataset.company  !== company)  pass = false;
      if (pass && event    !== 'ALL' && card.dataset.event    !== event)    pass = false;
      if (pass && category !== 'ALL' && card.dataset.category !== category) pass = false;
      if (pass && q        !== ''    && !card.dataset.title.includes(q))    pass = false;
      card.classList.toggle('hidden', !pass);
      if (pass) visible++;
    }
    visEl.textContent = visible;
    if (emptyEl) emptyEl.style.display = visible === 0 ? 'block' : 'none';
  }

  for (const el of [sigEl, isoEl, companyEl, eventEl, categoryEl]) {
    el.addEventListener('change', apply);
  }
  qEl.addEventListener('input', apply);

  resetBtn.addEventListener('click', function () {
    sigEl.value = 'high';
    isoEl.value = 'ALL';
    companyEl.value = 'ALL';
    eventEl.value = 'ALL';
    categoryEl.value = 'ALL';
    qEl.value = '';
    apply();
  });

  apply(); // initial pass — default 'high' (sig >= 4)
})();
"""


def build_html(summary: dict, articles: list, pipeline: list, developers: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    filter_opts = render_filter_options(articles)

    iso_select      = render_dropdown("iso",      filter_opts["iso"],      "All markets")
    company_select  = render_dropdown("company",  filter_opts["company"],  "All companies")
    event_select    = render_dropdown("event",    filter_opts["event"],    "All event types")
    category_select = render_dropdown("category", filter_opts["category"], "All categories")

    articles_html = "".join(render_article_card(a) for a in articles)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BESS Market Intelligence Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

  <header>
    <h1>BESS Market Intelligence</h1>
    <div class="tagline">Battery energy storage — competitive intelligence with ERCOT &amp; CAISO focus</div>
    <div class="updated">Last refreshed: {now}</div>
  </header>

  <div class="coverage-note">
    <strong>Scope:</strong> This dashboard covers <em>global</em> BESS news from 7 industry RSS feeds.
    The <strong>ERCOT</strong> and <strong>CAISO</strong> tags are applied only when an article specifically
    mentions those markets — most projects worldwide carry no ISO tag. Pipeline data (LBNL Queued Up) is
    US-only and scoped to ERCOT &amp; CAISO storage projects.
  </div>

  <h2>At a glance</h2>
  <section class="stats">
    <div class="stat"><div class="label">Articles tracked</div>
      <div class="value">{fmt_num(summary['articles'])}</div>
      <div class="sub">7 RSS sources, all topics</div></div>
    <div class="stat"><div class="label">Structured extractions</div>
      <div class="value">{fmt_num(summary['extracted'])}</div>
      <div class="sub">via Claude Opus 4.7</div></div>
    <div class="stat"><div class="label">High-significance (4+)</div>
      <div class="value">{fmt_num(summary['high_sig'])}</div>
      <div class="sub">market-relevant signals</div></div>
    <div class="stat"><div class="label">Active pipeline</div>
      <div class="value">{fmt_num(summary['pipeline_gw'])}</div>
      <div class="sub">GW — ERCOT + CAISO storage, active &amp; operational</div></div>
  </section>

  <h2>Article intelligence</h2>

  <details class="legend">
    <summary>Significance scoring guide (click to expand)</summary>
    <div class="scale">
      <div class="row"><span class="b badge-5">5</span><span><strong>Critical</strong> — market-moving event, major product launch, $500M+ deal, policy shift.</span></div>
      <div class="row"><span class="b badge-4">4</span><span><strong>High</strong> — contract wins, M&amp;A, notable competitor moves.</span></div>
      <div class="row"><span class="b badge-3">3</span><span><strong>Medium</strong> — industry analysis, technology updates, smaller deals.</span></div>
      <div class="row"><span class="b" style="background:#94a3b8;">2</span><span><strong>Low</strong> — adjacent market signals, EV supply chain, solar, grid infrastructure.</span></div>
      <div class="row"><span class="b" style="background:#cbd5e1;color:#475569;">1</span><span><strong>Monitoring</strong> — general energy news, low BESS specificity.</span></div>
    </div>
  </details>

  <div class="filters">
    <div class="filter-group">
      <label for="filter-sig">Significance</label>
      <select id="filter-sig">
        <option value="high">High (4+)</option>
        <option value="all">All (3+)</option>
        <option value="top">Top (5 only)</option>
      </select>
    </div>
    <div class="filter-group">
      <label for="filter-iso">ISO market</label>
      {iso_select}
    </div>
    <div class="filter-group">
      <label for="filter-company">Company</label>
      {company_select}
    </div>
    <div class="filter-group">
      <label for="filter-event">Event type</label>
      {event_select}
    </div>
    <div class="filter-group">
      <label for="filter-category">Category</label>
      {category_select}
    </div>
    <div class="filter-group">
      <label for="filter-q">Search titles</label>
      <input id="filter-q" type="search" placeholder="e.g. Spearmint, Megapack…" autocomplete="off">
    </div>
    <div class="filter-actions">
      <button id="reset-filters" class="reset-btn" type="button">Reset</button>
    </div>
  </div>

  <div class="count-bar">
    Showing <strong id="count-visible">0</strong> of
    <strong id="count-total">0</strong> articles
    <span style="color:var(--muted);"> (dataset: significance &ge; 3 only)</span>
  </div>

  <section id="articles-list">{articles_html}</section>
  <div id="empty-state" class="empty-state" style="display:none;">
    No articles match the current filters. Try widening significance or clearing filters.
  </div>

  <h2>Interconnection queue — by ISO &amp; status</h2>
  <section class="pipeline-grid">{render_pipeline_section(pipeline)}</section>
  <p style="font-size:11px; color: var(--muted); margin: 8px 0 0 4px;">
    Source: LBNL Queued Up 2026. GW = nameplate power capacity (no duration in dataset).</p>

  <h2>Top 10 named developers by queue capacity</h2>
  {render_developers_table(developers)}

  <footer>
    Generated from <code>data/intel.db</code> by <code>dashboard/build.py</code> ·
    Updated via GitHub Actions ·
    <a href="https://github.com/swati1395/bess-intelligence">source</a>
  </footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def build() -> None:
    conn = init_db()
    cur = conn.cursor()

    summary    = fetch_summary(cur)
    articles   = fetch_articles(cur)
    pipeline   = fetch_pipeline_by_iso(cur)
    developers = fetch_top_developers(cur)

    html_doc = build_html(summary, articles, pipeline, developers)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(html_doc)

    print(f"Wrote {OUTPUT_PATH}  ({len(html_doc):,} bytes)")
    print(f"  Articles in dataset: {len(articles)}  "
          f"(score 3+, default view filters to score 4+)")
    print(f"  Total tracked: {summary['articles']:,}  •  Extracted: {summary['extracted']:,}  •  "
          f"High-sig (4+): {summary['high_sig']}  •  Active pipeline: {summary['pipeline_gw']} GW")

    conn.close()


if __name__ == "__main__":
    build()
