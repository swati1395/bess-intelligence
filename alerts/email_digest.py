"""
BESS Intel — Email Alert System

# =============================================================================
# Gmail SMTP Setup
# =============================================================================
# This module sends alerts via Gmail SMTP. To enable:
#
# 1. On the Gmail account you want to send FROM, enable 2-Step Verification:
#    https://myaccount.google.com/security
#    (App passwords are unavailable until 2-Step Verification is on.)
#
# 2. Generate a Google App Password (NOT your regular password — Google
#    disabled SMTP with regular passwords in May 2022):
#    https://myaccount.google.com/apppasswords
#    Pick "Mail" + "Other (Custom name)" → name it "BESS Intel". Google will
#    show a 16-character password ONCE. Copy it immediately.
#
# 3. Add to your .env file at the project root:
#       GMAIL_SENDER=your.address@gmail.com
#       GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
#       ALERT_RECIPIENT=where.to.send@example.com
#
#    ALERT_RECIPIENT may be a single address or a comma-separated list, e.g.
#       ALERT_RECIPIENT=alice@example.com, bob@example.com
#    All addresses are Bcc'd (the message is addressed To the sender), so
#    subscribers cannot see one another's email addresses.
#
#    The app password may be displayed by Google with spaces every four
#    characters — either form works; smtplib strips them. Do not quote the
#    value in .env.
#
# 4. Verify with: python3 alerts/email_digest.py --test
#
# =============================================================================
# Scheduling
# =============================================================================
# This script sends ONE alert type per invocation. Wire up cron (or launchd
# on macOS) with the appropriate flag for each cadence:
#
#   # Tier 1 — hourly check for new 4/5 articles
#   0 * * * *  cd /path/to/project && python3 alerts/email_digest.py --tier1
#
#   # Daily digest — 8am local time
#   0 8 * * *  cd /path/to/project && python3 alerts/email_digest.py --daily
#
#   # Weekly roundup — Friday 5pm local time
#   0 17 * * 5 cd /path/to/project && python3 alerts/email_digest.py --weekly
#
# Tier 1 uses an `alerted_at` column to ensure each high-significance article
# is alerted exactly once, regardless of cron timing or partial failures.
# =============================================================================
"""

import argparse
import html
import os
import re
import smtplib
import sqlite3
import string
import sys
from datetime import datetime, timezone
from email.message import EmailMessage

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from database.setup_db import DB_PATH, init_db  # noqa: E402

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

ARTICLE_FIELDS = """
    ei.article_id, a.title, a.url, a.source_name, ei.extracted_at,
    ei.company, ei.event_type, ei.product_name, ei.capacity_mwh,
    ei.location_state, ei.iso_market, ei.customer,
    ei.significance_score, ei.significance_reason, ei.brief, ei.category
"""

ARTICLE_KEYS = [
    "article_id", "title", "url", "source_name", "extracted_at",
    "company", "event_type", "product_name", "capacity_mwh",
    "location_state", "iso_market", "customer",
    "significance_score", "significance_reason", "brief", "category",
]

# Mapping from category name to a short CSS slug for color-coded badges
CATEGORY_SLUGS = {
    "Deployment & Projects":        "deployment",
    "Supply Chain & Manufacturing": "supply",
    "Technology":                   "tech",
    "Policy & Regulation":          "policy",
    "Market & Commercial":          "market",
    "Applications & Adjacent":      "adjacent",
}

DASHBOARD_URL = "https://swati1395.github.io/bess-intelligence/"

CATEGORY_SHORT = {
    "Deployment & Projects":        "Deployment",
    "Supply Chain & Manufacturing": "Supply Chain",
    "Technology":                   "Tech",
    "Policy & Regulation":          "Policy",
    "Market & Commercial":          "Market",
    "Applications & Adjacent":      "Adjacent",
}

EVENT_SHORT = {
    "deployment":            "Deployment",
    "contract_announcement": "Contract",
    "merger_acquisition":    "M&A",
    "financing":             "Financing",
    "product_launch":        "Launch",
    "financial_results":     "Earnings",
    "partnership":           "Partnership",
    "regulatory_change":     "Regulatory",
    "analysis_commentary":   "Analysis",
    "other":                 "Other",
}

CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1a1a1a; margin: 0; padding: 0; background: #f3f4f6; }
  .cat-badge { display: inline-block; padding: 1px 7px; border-radius: 4px;
               font-size: 10px; font-weight: 600; margin-left: 6px;
               border: 1px solid currentColor; vertical-align: middle; }
  .cat-deployment { color: #1e40af; background: #dbeafe; border-color: #93c5fd; }
  .cat-supply   { color: #5b21b6; background: #ede9fe; border-color: #c4b5fd; }
  .cat-adjacent { color: #4d7c0f; background: #ecfccb; border-color: #bef264; }
  .cat-policy   { color: #9a3412; background: #ffedd5; border-color: #fdba74; }
  .cat-market   { color: #075985; background: #e0f2fe; border-color: #7dd3fc; }
  .cat-tech     { color: #831843; background: #fce7f3; border-color: #f9a8d4; }
  .legend-block { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
                  padding: 12px 16px; margin-bottom: 20px; font-size: 12px; }
  .legend-block .lt { font-weight: 600; color: #475569; margin-bottom: 6px;
                      text-transform: uppercase; letter-spacing: 0.04em; font-size: 11px; }
  .legend-block table { border-collapse: collapse; width: 100%; }
  .legend-block td { padding: 2px 8px 2px 0; vertical-align: top; color: #475569; line-height: 1.5; }
  .legend-block td.bn { white-space: nowrap; width: 1%; }
  .container { max-width: 640px; margin: 24px auto; padding: 28px; background: white;
               border-radius: 8px; }
  .header { border-bottom: 2px solid #1a1a1a; padding-bottom: 12px; margin-bottom: 20px; }
  .header h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.01em; }
  .header .meta { color: #6b7280; font-size: 13px; margin-top: 4px; }
  .group-header { font-weight: 600; font-size: 14px; color: #475569;
                  margin: 24px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #e2e8f0;
                  text-transform: uppercase; letter-spacing: 0.04em; }
  .article { border-left: 3px solid #2563eb; padding: 14px 16px;
             margin-bottom: 14px; background: #f8fafc; border-radius: 4px; }
  .article.score-4 { border-color: #ea580c; background: #fff7ed; }
  .article.score-5 { border-color: #dc2626; background: #fef2f2; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 11px; font-weight: 700; color: white; margin-right: 6px;
           vertical-align: middle; }
  .badge-3 { background: #ca8a04; }
  .badge-4 { background: #ea580c; }
  .badge-5 { background: #dc2626; }
  .article-title { font-weight: 600; font-size: 15px; margin: 0 0 6px; line-height: 1.35; }
  .article-title a { color: #1a1a1a; text-decoration: none; }
  .article-title a:hover { text-decoration: underline; }
  .article-meta { font-size: 12px; color: #6b7280; margin-bottom: 10px; }
  .fields { font-size: 13px; line-height: 1.6; margin-bottom: 8px; }
  .fields .row { margin: 0; }
  .fields .label { display: inline-block; min-width: 96px; color: #64748b;
                   font-weight: 500; }
  .significance { margin-top: 8px; padding: 8px 10px; background: white;
                  border-radius: 4px; font-size: 12.5px; color: #475569;
                  font-style: italic; }
  .link { margin-top: 10px; font-size: 13px; }
  .link a { color: #2563eb; text-decoration: none; }
  .footer { margin-top: 28px; padding-top: 14px; border-top: 1px solid #e2e8f0;
            font-size: 11px; color: #94a3b8; text-align: center; }
"""


def row_to_dict(row) -> dict:
    return dict(zip(ARTICLE_KEYS, row))


def fmt(value, dash="—") -> str:
    if value is None or value == "":
        return dash
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def fetch_tier1_pending(conn: sqlite3.Connection) -> list:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT {ARTICLE_FIELDS}
        FROM extracted_intel ei
        JOIN articles a ON a.id = ei.article_id
        WHERE ei.significance_score >= 4 AND ei.alerted_at IS NULL
        ORDER BY ei.significance_score DESC, ei.extracted_at DESC
        """
    )
    return [row_to_dict(r) for r in cur.fetchall()]


def fetch_daily(conn: sqlite3.Connection) -> list:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT {ARTICLE_FIELDS}
        FROM extracted_intel ei
        JOIN articles a ON a.id = ei.article_id
        WHERE ei.significance_score >= 3
          AND ei.extracted_at >= datetime('now', '-24 hours')
        ORDER BY ei.significance_score DESC, ei.extracted_at DESC
        """
    )
    return [row_to_dict(r) for r in cur.fetchall()]


def fetch_daily_low_count(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*)
        FROM extracted_intel ei
        WHERE ei.significance_score <= 2
          AND ei.extracted_at >= datetime('now', '-24 hours')
        """
    )
    return cur.fetchone()[0]


def fetch_weekly(conn: sqlite3.Connection, limit: int = 10) -> list:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT {ARTICLE_FIELDS}
        FROM extracted_intel ei
        JOIN articles a ON a.id = ei.article_id
        WHERE ei.extracted_at >= datetime('now', '-7 days')
        ORDER BY ei.significance_score DESC, ei.extracted_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [row_to_dict(r) for r in cur.fetchall()]


def fetch_test_sample(conn: sqlite3.Connection, n: int = 3) -> list:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT {ARTICLE_FIELDS}
        FROM extracted_intel ei
        JOIN articles a ON a.id = ei.article_id
        ORDER BY ei.significance_score DESC, ei.extracted_at DESC
        LIMIT ?
        """,
        (n,),
    )
    return [row_to_dict(r) for r in cur.fetchall()]


def group_by_company(articles: list) -> list:
    groups = {}
    for a in articles:
        key = a["company"] or "(no company identified)"
        groups.setdefault(key, []).append(a)
    return sorted(
        groups.items(),
        key=lambda kv: (-max(a["significance_score"] for a in kv[1]), -len(kv[1]), kv[0].lower()),
    )


def render_article_html(article: dict) -> str:
    score = article["significance_score"]
    score_class = f"score-{score}" if score >= 4 else ""
    badge_class = f"badge-{score}" if score in (3, 4, 5) else "badge-3"
    title = html.escape(article["title"] or "(untitled)")
    url = html.escape(article["url"] or "#", quote=True)
    source = html.escape(article["source_name"] or "")
    reason = html.escape(article["significance_reason"] or "")
    company = html.escape(fmt(article["company"]))
    event_type = html.escape(fmt(article["event_type"]))
    capacity = fmt(article["capacity_mwh"])
    capacity_html = html.escape(capacity if capacity == "—" else f"{capacity} MWh")
    iso_market = html.escape(fmt(article["iso_market"]))
    location = html.escape(fmt(article["location_state"]))
    customer = html.escape(fmt(article["customer"]))
    product = html.escape(fmt(article["product_name"]))
    extracted_at = html.escape(article["extracted_at"] or "")
    category = article.get("category")
    cat_html = ""
    if category and category in CATEGORY_SLUGS:
        cat_html = (f'<span class="cat-badge cat-{CATEGORY_SLUGS[category]}">'
                    f'{html.escape(category)}</span>')
    return f"""
    <div class="article {score_class}">
      <div class="article-title">
        <span class="badge {badge_class}">{score}/5</span>{cat_html}
        <a href="{url}">{title}</a>
      </div>
      <div class="article-meta">{source} • extracted {extracted_at} UTC</div>
      <div class="fields">
        <div class="row"><span class="label">Company:</span> {company}</div>
        <div class="row"><span class="label">Event:</span> {event_type}</div>
        <div class="row"><span class="label">Product:</span> {product}</div>
        <div class="row"><span class="label">Capacity:</span> {capacity_html}</div>
        <div class="row"><span class="label">Market:</span> {iso_market} ({location})</div>
        <div class="row"><span class="label">Customer:</span> {customer}</div>
      </div>
      <div class="significance">{reason}</div>
      <div class="link"><a href="{url}">Read article →</a></div>
    </div>
    """


LEGEND_HTML = """
<div class="legend-block">
  <div class="lt">Significance scoring</div>
  <table>
    <tr><td class="bn"><span class="badge badge-5">5</span></td><td><b>Critical</b> — market-moving event, major product launch, $500M+ deal, policy shift</td></tr>
    <tr><td class="bn"><span class="badge badge-4">4</span></td><td><b>High</b> — contract wins, M&amp;A, notable competitor moves</td></tr>
    <tr><td class="bn"><span class="badge badge-3">3</span></td><td><b>Medium</b> — industry analysis, technology updates, smaller deals</td></tr>
    <tr><td class="bn"><span class="badge" style="background:#94a3b8;">2</span></td><td><b>Low</b> — adjacent market signals, EV supply chain, solar, grid infrastructure</td></tr>
    <tr><td class="bn"><span class="badge" style="background:#cbd5e1;color:#475569;">1</span></td><td><b>Monitoring</b> — general energy news, low BESS specificity</td></tr>
  </table>
</div>
"""

LEGEND_PLAIN = """\
Significance scoring:
  5 = Critical    — market-moving event, major product launch, $500M+ deal, policy shift
  4 = High        — contract wins, M&A, notable competitor moves
  3 = Medium      — industry analysis, technology updates, smaller deals
  2 = Low         — adjacent market signals, EV supply chain, solar, grid infrastructure
  1 = Monitoring  — general energy news, low BESS specificity
"""


def render_html(title: str, subtitle: str, sections: list) -> str:
    body_parts = [LEGEND_HTML]
    for header, articles in sections:
        if header:
            body_parts.append(f'<div class="group-header">{html.escape(header)}</div>')
        for a in articles:
            body_parts.append(render_article_html(a))
        if not articles:
            body_parts.append(
                '<div class="article" style="border-color: #94a3b8; background: #f8fafc;">'
                '<div class="article-meta">No qualifying articles in this period.</div>'
                '</div>'
            )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>{CSS}</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{html.escape(title)}</h1>
    <div class="meta">{html.escape(subtitle)}</div>
  </div>
  {''.join(body_parts)}
  <div class="footer">BESS Market Intelligence System • automated alert</div>
</div>
</body>
</html>"""


def render_plain(title: str, subtitle: str, sections: list) -> str:
    lines = [title, subtitle, "=" * len(title), "", LEGEND_PLAIN]
    for header, articles in sections:
        if header:
            lines.append(f"--- {header} ---")
        if not articles:
            lines.append("  (no qualifying articles)")
        for a in articles:
            cap = fmt(a["capacity_mwh"])
            cap_str = cap if cap == "—" else f"{cap} MWh"
            cat = a.get("category") or "—"
            lines.append(f"  [{a['significance_score']}/5] [{cat}] {a['title']}")
            lines.append(f"        Source: {a['source_name']}  •  Company: {fmt(a['company'])}")
            lines.append(f"        Event: {fmt(a['event_type'])}  •  Capacity: {cap_str}  •  Market: {fmt(a['iso_market'])}")
            lines.append(f"        {a['significance_reason']}")
            lines.append(f"        {a['url']}")
            lines.append("")
        lines.append("")
    lines.append("— BESS Market Intelligence System (automated alert)")
    return "\n".join(lines)


# =============================================================================
# Daily digest — tiered layout with dedup
# =============================================================================

def _title_tokens(title: str) -> set:
    t = (title or "").lower().translate(str.maketrans("", "", string.punctuation))
    return set(t.split())


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_articles(articles: list) -> list:
    """
    Collapse same-event articles into clusters.

    Two articles match if they share the same non-empty company (case-insensitive)
    AND either the same non-null capacity_mwh OR title Jaccard >= 0.6.
    When in doubt, don't merge.

    Returns list of {"primary": article_dict, "sources": [(name, url), ...]}
    """
    THRESHOLD = 0.6
    clusters = []
    used = [False] * len(articles)

    for i, a in enumerate(articles):
        if used[i]:
            continue
        used[i] = True
        members = [a]
        company_a = (a["company"] or "").strip().lower()
        cap_a = a["capacity_mwh"]
        tokens_a = _title_tokens(a["title"])

        for j, b in enumerate(articles):
            if i == j or used[j]:
                continue
            company_b = (b["company"] or "").strip().lower()
            if not company_a or not company_b or company_a != company_b:
                continue
            cap_b = b["capacity_mwh"]
            if cap_a is not None and cap_b is not None:
                matches = (cap_a == cap_b)
            else:
                matches = _jaccard(tokens_a, _title_tokens(b["title"])) >= THRESHOLD
            if matches:
                used[j] = True
                members.append(b)

        primary = max(
            members,
            key=lambda x: (
                x["significance_score"],
                len(x["significance_reason"] or ""),
                x["extracted_at"] or "",
            ),
        )
        seen_urls: set = set()
        sources = []
        for m in members:
            if m["url"] not in seen_urls:
                seen_urls.add(m["url"])
                sources.append((m["source_name"], m["url"]))

        clusters.append({"primary": primary, "sources": sources})

    return clusters


def _make_tag_html(category: str | None, event_type: str | None) -> str:
    parts = []
    if category:
        parts.append(CATEGORY_SHORT.get(category, category))
    if event_type:
        parts.append(EVENT_SHORT.get(event_type, event_type))
    if not parts:
        return ""
    label = html.escape(" · ".join(parts))
    return (
        f'<span style="display:inline-block;padding:1px 7px;border-radius:4px;'
        f'font-size:11px;font-weight:500;color:#64748b;background:#f1f5f9;'
        f'border:1px solid #e2e8f0;vertical-align:middle;margin-left:6px;">'
        f'{label}</span>'
    )


def _sources_html(sources: list) -> str:
    parts = []
    for name, url in sources:
        safe_url = html.escape(url or "#", quote=True)
        safe_name = html.escape(name or "Source")
        parts.append(
            f'<a href="{safe_url}" style="color:#2563eb;text-decoration:none;">'
            f'{safe_name}</a>'
        )
    return " · ".join(parts)


def _first_sentence(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\.(\s|$)", text)
    return text[: m.start() + 1].strip() if m else text.strip()


_CARD_BASE = "border-radius:4px;margin-bottom:12px;"
_SEC_HDR = (
    'style="font-weight:700;font-size:12px;color:#475569;margin:28px 0 10px;'
    'padding-bottom:5px;border-bottom:2px solid #e2e8f0;'
    'text-transform:uppercase;letter-spacing:0.06em;"'
)


def _render_top_story(cluster: dict) -> str:
    p = cluster["primary"]
    title = html.escape(p["title"] or "(untitled)")
    tag = _make_tag_html(p.get("category"), p.get("event_type"))
    body = html.escape(p.get("brief") or p.get("significance_reason") or "")
    srcs = _sources_html(cluster["sources"])
    return (
        f'<div style="border-left:4px solid #dc2626;padding:14px 16px;'
        f'background:#fef2f2;{_CARD_BASE}">'
        f'<div style="font-weight:700;font-size:16px;line-height:1.35;margin-bottom:7px;">'
        f'{title}{tag}</div>'
        f'<div style="font-size:13px;color:#374151;line-height:1.55;margin-bottom:9px;">'
        f'{body}</div>'
        f'<div style="font-size:13px;">Read more → {srcs}</div>'
        f'</div>'
    )


def _render_high_story(cluster: dict) -> str:
    p = cluster["primary"]
    title = html.escape(p["title"] or "(untitled)")
    tag = _make_tag_html(p.get("category"), p.get("event_type"))
    body = html.escape(
        p.get("brief") or _first_sentence(p.get("significance_reason") or "")
    )
    srcs = _sources_html(cluster["sources"])
    return (
        f'<div style="border-left:3px solid #ea580c;padding:12px 16px;'
        f'background:#fff7ed;{_CARD_BASE}">'
        f'<div style="font-weight:600;font-size:15px;line-height:1.35;margin-bottom:5px;">'
        f'{title}{tag}</div>'
        f'<div style="font-size:13px;color:#374151;margin-bottom:7px;">{body}</div>'
        f'<div style="font-size:13px;">Read more → {srcs}</div>'
        f'</div>'
    )


def _render_glance_story(cluster: dict) -> str:
    p = cluster["primary"]
    title = html.escape(p["title"] or "(untitled)")
    url = html.escape(p["url"] or "#", quote=True)
    srcs = _sources_html(cluster["sources"])
    # Two-cell table: bullet locked in a fixed-width cell so wrapped lines
    # stay in the text cell and align under the first line of text, not the bullet.
    # table-layout:fixed + explicit widths prevent Gmail from collapsing the bullet cell.
    return (
        '<div style="border-bottom:1px solid #f1f5f9;">'
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"'
        ' style="table-layout:fixed;width:100%;">'
        '<tr>'
        '<td width="22" valign="top"'
        ' style="width:22px;min-width:22px;padding:6px 6px 6px 0;'
        'font-size:16px;line-height:1.5;color:#378ADD;vertical-align:top;'
        'white-space:nowrap;">&#x25CF;</td>'
        f'<td style="padding:6px 0;font-size:13px;line-height:1.5;word-break:break-word;">'
        f'<a href="{url}" style="color:#1a1a1a;text-decoration:none;">{title}</a>'
        f'&ensp;&rarr;&ensp;{srcs}'
        f'</td>'
        '</tr>'
        '</table>'
        '</div>'
    )


def _render_tiered_html(
    title: str, subtitle: str,
    clusters_by_sig: dict, low_count: int,
    date_long: str = "",
) -> str:
    parts = []
    top = clusters_by_sig.get(5, [])
    high = clusters_by_sig.get(4, [])
    glance = clusters_by_sig.get(3, [])

    if top:
        parts.append(f'<div {_SEC_HDR}>Top Stories</div>')
        parts.extend(_render_top_story(c) for c in top)
    if high:
        parts.append(f'<div {_SEC_HDR}>What Else Moved</div>')
        parts.extend(_render_high_story(c) for c in high)
    if glance:
        parts.append(f'<div {_SEC_HDR}>Worth a Glance</div>')
        parts.extend(_render_glance_story(c) for c in glance)
    if low_count:
        dash = html.escape(DASHBOARD_URL, quote=True)
        parts.append(
            f'<div style="margin-top:24px;padding:14px 16px;background:#f8fafc;'
            f'border-radius:4px;border:1px solid #e2e8f0;">'
            f'<div style="font-weight:700;font-size:12px;color:#475569;'
            f'text-transform:uppercase;letter-spacing:0.06em;margin-bottom:6px;">'
            f'More in the Dashboard</div>'
            f'<div style="font-size:13px;color:#374151;line-height:1.55;margin-bottom:8px;">'
            f'<b>{low_count}</b> more signals today — adjacent markets, EV supply chain, '
            f'solar, grid infrastructure, and general energy news.</div>'
            f'<div style="font-size:13px;">'
            f'Browse everything, search the full archive, and revisit past days → '
            f'<a href="{dash}" style="color:#2563eb;">dashboard</a>'
            f'</div>'
            f'</div>'
        )

    body = "\n".join(parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BESS Intel</title>
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#1a1a1a;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" role="presentation"
       style="background:#f3f4f6;">
  <tr>
    <td align="center" style="padding:24px 16px;">

      <!--[if mso]><table width="640" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
      <table width="640" cellpadding="0" cellspacing="0" border="0" role="presentation"
             style="max-width:640px;width:100%;border-radius:8px;background:#ffffff;">

        <!-- MASTHEAD -->
        <tr>
          <td bgcolor="#042C53"
              style="background-color:#042C53;padding:20px 24px;
                     border-bottom:3px solid #378ADD;border-radius:8px 8px 0 0;">
            <p style="margin:0 0 6px;font-size:22px;font-weight:500;color:#ffffff;
                      letter-spacing:0.03em;line-height:1.2;">
              <span style="color:#85B7EB;">&#x26A1;</span>&nbsp;Battery Energy Storage Brief
            </p>
            <p style="margin:0;font-size:13px;color:#B5D4F4;line-height:1.4;">
              Daily battery storage market brief &middot; {html.escape(date_long)}
            </p>
          </td>
        </tr>

        <!-- SUBTITLE STRIP -->
        <tr>
          <td style="background-color:#ffffff;padding:11px 24px;
                     border-bottom:1px solid #e2e8f0;">
            <span style="font-size:12px;color:#6b7280;">{html.escape(subtitle)}</span>
          </td>
        </tr>

        <!-- CONTENT -->
        <tr>
          <td style="background-color:#ffffff;padding:22px 24px 28px;
                     border-radius:0 0 8px 8px;">
            {body}
            <div style="margin-top:28px;padding-top:14px;border-top:1px solid #e2e8f0;
                        font-size:11px;color:#94a3b8;text-align:center;">
              Battery Energy Storage Brief &middot; automated digest
            </div>
          </td>
        </tr>

      </table>
      <!--[if mso]></td></tr></table><![endif]-->

    </td>
  </tr>
</table>
</body>
</html>"""


def _render_tiered_plain(
    title: str, subtitle: str,
    clusters_by_sig: dict, low_count: int,
) -> str:
    lines = [title, subtitle, "=" * max(len(title), len(subtitle)), ""]

    def _tag(p):
        parts = []
        if p.get("category"):
            parts.append(CATEGORY_SHORT.get(p["category"], p["category"]))
        if p.get("event_type"):
            parts.append(EVENT_SHORT.get(p["event_type"], p["event_type"]))
        return f"[{' · '.join(parts)}]" if parts else ""

    def _srcs(sources):
        return " · ".join(name for name, _ in sources)

    top = clusters_by_sig.get(5, [])
    high = clusters_by_sig.get(4, [])
    glance = clusters_by_sig.get(3, [])

    if top:
        lines += ["TOP STORIES", "─" * 40]
        for i, c in enumerate(top, 1):
            p = c["primary"]
            merged = f" [{len(c['sources'])} sources]" if len(c["sources"]) > 1 else ""
            lines += [
                f"  {i}. {p['title']}  {_tag(p)}{merged}",
                f'     "{p["significance_reason"] or ""}"',
                f"     → {_srcs(c['sources'])}",
                "",
            ]
    if high:
        lines += ["WHAT ELSE MOVED", "─" * 40]
        for i, c in enumerate(high, 1):
            p = c["primary"]
            lines += [
                f"  {i}. {p['title']}  {_tag(p)}",
                f'     "{_first_sentence(p["significance_reason"] or "")}"',
                f"     → {_srcs(c['sources'])}",
                "",
            ]
    if glance:
        lines += ["WORTH A GLANCE", "─" * 40]
        for i, c in enumerate(glance, 1):
            p = c["primary"]
            lines.append(
                f"  {i:>2}. {p['title']}  {_tag(p)}  → {_srcs(c['sources'])}"
            )
        lines.append("")
    if low_count:
        lines += [
            "MORE IN THE DASHBOARD",
            f"{low_count} more signals today — adjacent markets, EV supply chain, solar, "
            f"grid infrastructure, and general energy news.",
            f"Browse everything, search the full archive, and revisit past days → {DASHBOARD_URL}",
        ]
    lines += ["", "— BESS Market Intelligence System (automated digest)"]
    return "\n".join(lines)


def _build_daily_subject(clusters_by_sig: dict, date_str: str) -> str:
    return f"Battery Energy Storage Brief — {date_str}"


# =============================================================================
# Email composition
# =============================================================================

def send_email(sender: str, password: str, recipients: list,
               subject: str, html_body: str, plain_body: str) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    # Address the message to ourselves and Bcc every subscriber, so recipients
    # cannot see each other's addresses. smtplib.send_message() strips the Bcc
    # header before transmission but still delivers to those addresses.
    msg["To"] = sender
    msg["Bcc"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")
    clean_password = "".join(password.split())  # strips any whitespace incl. U+00A0
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(sender, clean_password)
        smtp.send_message(msg)


def mark_alerted(conn: sqlite3.Connection, article_ids: list) -> None:
    if not article_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "UPDATE extracted_intel SET alerted_at = ? WHERE article_id = ?",
        [(now, aid) for aid in article_ids],
    )
    conn.commit()


def require_env() -> tuple:
    sender = os.environ.get("GMAIL_SENDER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient_raw = os.environ.get("ALERT_RECIPIENT")
    missing = [k for k, v in
               (("GMAIL_SENDER", sender), ("GMAIL_APP_PASSWORD", password),
                ("ALERT_RECIPIENT", recipient_raw)) if not v]
    if missing:
        sys.exit(f"Missing required env vars in .env: {', '.join(missing)}\n"
                 f"See the comment block at the top of alerts/email_digest.py for setup.")
    # ALERT_RECIPIENT may be a single address or a comma-separated list.
    recipients = [addr.strip() for addr in recipient_raw.split(",") if addr.strip()]
    if not recipients:
        sys.exit("ALERT_RECIPIENT is set but contains no valid email addresses.")
    return sender, password, recipients


def build_alert(mode: str, conn: sqlite3.Connection):
    """Returns (subject, html, plain, article_ids_to_mark) or None to skip."""
    now_local = datetime.now()
    if mode == "tier1":
        articles = fetch_tier1_pending(conn)
        if not articles:
            return None
        n = len(articles)
        subject = (f"[BESS Alert] {n} new high-significance "
                   f"article{'s' if n != 1 else ''}")
        title = f"BESS Tier 1 Alert — {n} new article{'s' if n != 1 else ''}"
        subtitle = f"Significance 4-5, sent {now_local.strftime('%Y-%m-%d %H:%M %Z').strip()}"
        sections = [("New high-significance articles", articles)]
        ids = [a["article_id"] for a in articles]
        return subject, render_html(title, subtitle, sections), render_plain(title, subtitle, sections), ids

    if mode == "daily":
        articles = fetch_daily(conn)
        low_count = fetch_daily_low_count(conn)
        clusters = cluster_articles(articles)

        clusters_by_sig: dict = {5: [], 4: [], 3: []}
        for c in clusters:
            sig = c["primary"]["significance_score"]
            if sig in clusters_by_sig:
                clusters_by_sig[sig].append(c)

        sig3_cap = 5
        sig3_overflow = max(0, len(clusters_by_sig[3]) - sig3_cap)
        clusters_by_sig[3] = clusters_by_sig[3][:sig3_cap]
        low_count += sig3_overflow
        date_str = now_local.strftime("%b %-d")
        date_long = now_local.strftime("%B %-d, %Y")
        subject = _build_daily_subject(clusters_by_sig, date_str)
        title = f"BESS Intel — {now_local.strftime('%Y-%m-%d')}"
        n_notable = sum(len(v) for v in clusters_by_sig.values())
        subtitle = (
            f"{n_notable} notable stor{'ies' if n_notable != 1 else 'y'} · "
            f"{low_count} lower-signal items in dashboard"
        )
        html_body = _render_tiered_html(title, subtitle, clusters_by_sig, low_count, date_long)
        plain_body = _render_tiered_plain(title, subtitle, clusters_by_sig, low_count)
        return subject, html_body, plain_body, []

    if mode == "weekly":
        articles = fetch_weekly(conn, limit=10)
        end = now_local
        start = datetime.fromtimestamp(end.timestamp() - 7 * 86400)
        range_str = f"{start.strftime('%Y-%m-%d')} — {end.strftime('%Y-%m-%d')}"
        subject = f"BESS Intel Weekly: {range_str}"
        title = subject
        subtitle = f"Top {len(articles)} most significant article{'s' if len(articles) != 1 else ''} of the past 7 days"
        sections = [("Top 10 by significance", articles)]
        return subject, render_html(title, subtitle, sections), render_plain(title, subtitle, sections), []

    if mode == "test":
        articles = fetch_test_sample(conn, n=3)
        if not articles:
            sys.exit("No extracted articles in the database — run the extractor first.")
        subject = f"[BESS Intel TEST] Sample alert with {len(articles)} articles"
        title = "BESS Intel — Test Email"
        subtitle = (f"Sent {now_local.strftime('%Y-%m-%d %H:%M %Z').strip()} • "
                    f"3 sample articles, ordered by significance")
        sections = [("Top sample articles", articles)]
        return subject, render_html(title, subtitle, sections), render_plain(title, subtitle, sections), []

    raise ValueError(f"Unknown mode: {mode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("# ===")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tier1", action="store_true",
                       help="Send immediate alerts for unalerted articles with score 4-5.")
    group.add_argument("--daily", action="store_true",
                       help="Send daily digest of last 24h, significance ≥ 3, tiered by score.")
    group.add_argument("--weekly", action="store_true",
                       help="Send weekly roundup of top 10 by significance from last 7 days.")
    group.add_argument("--test", action="store_true",
                       help="Send a test email containing 3 sample articles. Does not mark anything as alerted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and print the email body, but don't send.")
    args = parser.parse_args()

    mode = next(m for m in ("tier1", "daily", "weekly", "test") if getattr(args, m))
    conn = init_db()

    alert = build_alert(mode, conn)
    if alert is None:
        print(f"[{mode}] Nothing to send — no qualifying articles.")
        conn.close()
        return

    subject, html_body, plain_body, mark_ids = alert

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print("--- PLAIN TEXT ---\n")
        print(plain_body)
        print(f"\n--- HTML LENGTH: {len(html_body)} chars ---")
        if mark_ids:
            print(f"\n(Would mark {len(mark_ids)} article(s) as alerted)")
        conn.close()
        return

    sender, password, recipients = require_env()
    print(f"[{mode}] Sending to {len(recipients)} recipient(s) "
          f"({', '.join(recipients)}) via {sender} — subject: {subject!r}")
    try:
        send_email(sender, password, recipients, subject, html_body, plain_body)
    except smtplib.SMTPAuthenticationError as e:
        sys.exit(f"SMTP authentication failed: {e}\n"
                 f"Double-check GMAIL_APP_PASSWORD (must be a 16-char App Password, "
                 f"not your Google account password). See setup at top of this file.")
    except smtplib.SMTPException as e:
        sys.exit(f"SMTP error sending email: {e}")

    if mark_ids:
        mark_alerted(conn, mark_ids)
        print(f"[{mode}] Marked {len(mark_ids)} article(s) as alerted.")
    print(f"[{mode}] Sent.")
    conn.close()


if __name__ == "__main__":
    main()
