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
from email.utils import parsedate_to_datetime
from html import escape
from zoneinfo import ZoneInfo

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
    """Render any reasonably-shaped date string as 'D MMM YYYY' (e.g. '13 Jun 2025').
    Tries ISO 8601 first, then 'Month D, YYYY', then 'YYYY.MM.DD', then RFC 2822.
    Returns the original (truncated) string if nothing parses."""
    if not value:
        return ""
    s = str(value).strip()
    # ISO 8601 (most common — published_at column stores this)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.strftime("%-d %b %Y")
        except ValueError:
            pass
    # Common human-readable formats
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y.%m.%d", "%d %b %Y", "%m/%d/%Y"):
        try:
            d = datetime.strptime(s, fmt)
            return d.strftime("%-d %b %Y")
        except ValueError:
            continue
    # RFC 2822 (raw RSS pubDate)
    try:
        d = parsedate_to_datetime(s)
        if d is not None:
            return d.strftime("%-d %b %Y")
    except (TypeError, ValueError):
        pass
    return s[:20]


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
        WHERE ei.significance_score >= 1
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
        <thead><tr><th>#</th><th>Developer</th><th class='num'>Projects</th><th class='num'>GW</th><th class='num'>ERCOT | CAISO</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    <p style="font-size:11px; color: var(--text-secondary); margin: 6px 0 0 4px;">
      Format: projects in ERCOT / projects in CAISO
    </p>
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
  --bg:             #F2F2F7;
  --card:           #FFFFFF;
  --border:         #E5E5EA;
  --text-primary:   #1D1D1F;
  --text-secondary: #8E8E93;
  --text-tertiary:  #636366;
  --accent:         #007AFF;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text",
               "Helvetica Neue", system-ui, sans-serif;
  background: var(--bg); color: var(--text-primary);
  line-height: 1.5; font-size: 15px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
.container { max-width: 1100px; margin: 0 auto; padding: 56px 24px 96px; }

/* Header */
header { margin-bottom: 40px; padding-bottom: 28px; border-bottom: 1px solid var(--border); }
header h1 { margin: 0; font-size: 34px; font-weight: 700; letter-spacing: -0.022em; line-height: 1.15; }
header .tagline { color: var(--text-secondary); font-size: 16px; margin-top: 8px;
                  letter-spacing: -0.005em; }
header .updated { color: var(--text-secondary); font-size: 12px; margin-top: 14px;
                  font-family: ui-monospace, "SF Mono", Menlo, monospace; }

/* Coverage note */
.coverage-note { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
                 padding: 18px 22px; font-size: 13px; color: var(--text-tertiary);
                 margin-bottom: 40px; line-height: 1.55; }
.coverage-note strong { color: var(--text-primary); font-weight: 600; }

/* Section headers */
h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--text-secondary); margin: 48px 0 16px; font-weight: 600; }

/* Generic card surface */
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
        padding: 24px; margin-bottom: 12px; }

/* Stat cards */
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.stat { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
        padding: 24px; }
.stat .label { font-size: 11px; text-transform: uppercase; color: var(--text-secondary);
               letter-spacing: 0.08em; font-weight: 600; margin-bottom: 8px; }
.stat .value { font-size: 38px; font-weight: 700; letter-spacing: -0.025em; line-height: 1.05;
               color: var(--text-primary); }
.stat .sub { font-size: 12px; color: var(--text-secondary); margin-top: 6px;
             line-height: 1.45; }

/* Legend */
.legend { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
          padding: 18px 22px; margin-bottom: 16px; }
.legend summary { cursor: pointer; font-weight: 600; font-size: 14px;
                  color: var(--text-primary); list-style: none;
                  display: flex; align-items: center; gap: 8px;
                  letter-spacing: -0.005em; }
.legend summary::-webkit-details-marker { display: none; }
.legend summary::after { content: "›"; color: var(--text-secondary); margin-left: auto;
                         font-size: 20px; line-height: 1; transition: transform 0.18s ease; }
.legend[open] summary::after { transform: rotate(90deg); }
.legend .scale { margin-top: 18px; display: grid; gap: 10px; }
.legend .row { display: grid; grid-template-columns: 36px 1fr; align-items: center;
               font-size: 13px; line-height: 1.5; color: var(--text-tertiary); }
.legend .row strong { color: var(--text-primary); font-weight: 600; }
.legend .row .b { display: inline-block; min-width: 28px; text-align: center;
                  padding: 2px 0; border-radius: 5px; font-size: 11px; font-weight: 700;
                  margin-right: 12px; }

/* Filters */
.filters { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
           padding: 22px; margin-bottom: 16px;
           display: grid; gap: 18px;
           grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.filter-group { display: flex; flex-direction: column; gap: 6px; }
.filter-group label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
                      color: var(--text-secondary); font-weight: 600; }
.filter-group select, .filter-group input {
  font: inherit; font-size: 14px;
  padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--card); color: var(--text-primary); width: 100%;
  -webkit-appearance: none; appearance: none;
  transition: border-color 0.15s ease;
}
.filter-group select {
  background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 8" fill="%238E8E93"><path d="M6 8 0 0h12z"/></svg>');
  background-repeat: no-repeat; background-position: right 12px center;
  background-size: 10px 6px; padding-right: 32px;
}
.filter-group select:focus, .filter-group input:focus {
  outline: none; border-color: var(--accent);
}
.filter-group input::placeholder { color: var(--text-secondary); }

.filter-actions { display: flex; align-items: end; gap: 8px; }
button.reset-btn { font: inherit; font-size: 14px; padding: 9px 18px;
                   border: 1px solid var(--border); border-radius: 8px;
                   background: var(--card); color: var(--accent); cursor: pointer;
                   font-weight: 500; transition: background-color 0.15s ease; }
button.reset-btn:hover { background: var(--bg); }

/* Count bar */
.count-bar { font-size: 13px; color: var(--text-secondary);
             margin: 8px 0 24px; font-variant-numeric: tabular-nums; }
.count-bar strong { color: var(--text-primary); font-weight: 600; }

/* Article cards */
.article-card { background: var(--card); border: 1px solid var(--border);
                border-radius: 12px; padding: 24px; margin-bottom: 12px; }
.article-card.hidden { display: none; }
.article-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
                flex-wrap: wrap; }
.article-title { font-weight: 700; font-size: 15px; color: var(--text-primary);
                 text-decoration: none; line-height: 1.4; letter-spacing: -0.01em; }
.article-title:hover { color: var(--accent); }
.article-meta { font-size: 12px; color: #8E8E93; margin-bottom: 14px; line-height: 1.55; }
.article-meta span { margin-right: 2px; }
.article-reason { margin: 0; font-size: 13px; color: #636366; font-style: italic;
                  line-height: 1.55; }

.empty-state { padding: 56px 24px; text-align: center; color: var(--text-secondary);
               font-size: 14px; border: 1px solid var(--border); border-radius: 12px;
               background: var(--card); }

/* Significance badges — solid fill, semantic Apple colors */
.badge { display: inline-block; padding: 2px 8px; border-radius: 5px;
         font-size: 11px; font-weight: 700; vertical-align: middle;
         line-height: 1.4; letter-spacing: 0; }
.badge-1 { background: #C7C7CC; color: #1D1D1F; }
.badge-2 { background: #8E8E93; color: #FFFFFF; }
.badge-3 { background: #FFCC00; color: #1D1D1F; }
.badge-4 { background: #FF9500; color: #FFFFFF; }
.badge-5 { background: #FF3B30; color: #FFFFFF; }

/* Category badges — outlined pill, colored text on transparent */
.cat-badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
             font-size: 11px; font-weight: 600; background: transparent;
             border: 1px solid currentColor; vertical-align: middle;
             white-space: nowrap; margin-left: 6px; line-height: 1.5; }
.cat-direct   { color: #007AFF; }
.cat-supply   { color: #5856D6; }
.cat-adjacent { color: #34C759; }
.cat-policy   { color: #FF9500; }
.cat-market   { color: #5AC8FA; }
.cat-mna      { color: #FF2D55; }

/* Pipeline */
.pipeline-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.pipeline-iso { padding: 24px; }
.pipeline-iso h3 { margin: 0 0 16px; font-size: 17px; font-weight: 700;
                   letter-spacing: -0.018em; color: var(--text-primary); }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { color: var(--text-secondary); font-weight: 600; font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.06em; }
td { color: var(--text-primary); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:last-child td { border-bottom: none; }
tfoot th { border-bottom: none; border-top: 1px solid var(--border); color: var(--text-primary); }

.developers tbody tr td:nth-child(1) { color: var(--text-secondary); width: 32px; }
.developers tbody tr td:nth-child(2) { font-weight: 500; }

/* Footer */
footer { margin-top: 72px; padding-top: 24px; border-top: 1px solid var(--border);
         font-size: 12px; color: var(--text-secondary); text-align: center;
         line-height: 1.6; }
footer code { font-family: ui-monospace, "SF Mono", Menlo, monospace;
              color: var(--text-tertiary); font-size: 11px; }
footer a { color: var(--accent); text-decoration: none; }
footer a:hover { text-decoration: underline; }

@media (max-width: 720px) {
  .container { padding: 32px 16px 64px; }
  .stats { grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat { padding: 18px; }
  .stat .value { font-size: 30px; }
  .pipeline-grid { grid-template-columns: 1fr; }
  header h1 { font-size: 26px; }
  .filter-actions { justify-content: stretch; }
  button.reset-btn { flex: 1; }
  .article-card, .legend, .filters, .coverage-note, .card { padding: 18px; }
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
    if (val === 'all')    return true;
    if (val === 'medium') return parseInt(card.dataset.score, 10) >= 3;
    return card.dataset.score === val; // exact-match: '1'..'5'
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
    sigEl.value = 'medium';
    isoEl.value = 'ALL';
    companyEl.value = 'ALL';
    eventEl.value = 'ALL';
    categoryEl.value = 'ALL';
    qEl.value = '';
    apply();
  });

  apply(); // initial pass — default 'medium' (sig >= 3)
})();
"""


def build_html(summary: dict, articles: list, pipeline: list, developers: list) -> str:
    now = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%-d %b %Y, %H:%M %Z")
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
    <strong>Scope:</strong> This dashboard covers <em>global</em> BESS news from 22 sources including RSS feeds and company newsrooms.
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
      <div class="sub">via Claude Sonnet</div></div>
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
      <div class="row"><span class="b badge-2">2</span><span><strong>Low</strong> — adjacent market signals, EV supply chain, solar, grid infrastructure.</span></div>
      <div class="row"><span class="b badge-1">1</span><span><strong>Monitoring</strong> — general energy news, low BESS specificity.</span></div>
    </div>
  </details>

  <div class="filters">
    <div class="filter-group">
      <label for="filter-sig">Significance</label>
      <select id="filter-sig">
        <option value="medium">Medium+ (3-5)</option>
        <option value="all">All (1-5)</option>
        <option value="5">5 — Critical</option>
        <option value="4">4 — High</option>
        <option value="3">3 — Medium</option>
        <option value="2">2 — Low</option>
        <option value="1">1 — Monitoring</option>
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
    <span style="color:var(--text-secondary);"> (all extracted articles, significance 1-5)</span>
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
          f"(all scores 1-5, default view shows all)")
    print(f"  Total tracked: {summary['articles']:,}  •  Extracted: {summary['extracted']:,}  •  "
          f"High-sig (4+): {summary['high_sig']}  •  Active pipeline: {summary['pipeline_gw']} GW")

    conn.close()


if __name__ == "__main__":
    build()
