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
import smtplib
import sqlite3
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
    ei.significance_score, ei.significance_reason, ei.category
"""

ARTICLE_KEYS = [
    "article_id", "title", "url", "source_name", "extracted_at",
    "company", "event_type", "product_name", "capacity_mwh",
    "location_state", "iso_market", "customer",
    "significance_score", "significance_reason", "category",
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

CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #1a1a1a; margin: 0; padding: 0; background: #f3f4f6; }
  .cat-badge { display: inline-block; padding: 1px 7px; border-radius: 4px;
               font-size: 10px; font-weight: 600; margin-left: 6px;
               border: 1px solid currentColor; vertical-align: middle; }
  .cat-direct   { color: #1e40af; background: #dbeafe; border-color: #93c5fd; }
  .cat-supply   { color: #5b21b6; background: #ede9fe; border-color: #c4b5fd; }
  .cat-adjacent { color: #4d7c0f; background: #ecfccb; border-color: #bef264; }
  .cat-policy   { color: #9a3412; background: #ffedd5; border-color: #fdba74; }
  .cat-market   { color: #075985; background: #e0f2fe; border-color: #7dd3fc; }
  .cat-mna      { color: #831843; background: #fce7f3; border-color: #f9a8d4; }
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
        date_str = now_local.strftime("%Y-%m-%d")
        subject = f"BESS Intel Daily: {date_str}"
        title = subject
        subtitle = (f"{len(articles)} article{'s' if len(articles) != 1 else ''} "
                    f"with significance ≥ 3 in the last 24 hours")
        sections = group_by_company(articles)
        return subject, render_html(title, subtitle, sections), render_plain(title, subtitle, sections), []

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
                       help="Send daily digest of last 24h, significance ≥ 3, grouped by company.")
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
