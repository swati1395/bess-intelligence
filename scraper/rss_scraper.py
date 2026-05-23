import calendar
import json
import os
import re
import sqlite3
import ssl
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    ssl._create_default_https_context = lambda *a, **kw: ssl.create_default_context(
        cafile=certifi.where(), *a, **kw
    )
except ImportError:
    pass

import feedparser
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from database.setup_db import DB_PATH, init_db  # noqa: E402

CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "sources.yaml")

TAG_KEYWORDS = {
    "ERCOT":      ["Texas", "ERCOT", "ECRS", "RRS", "CPS Energy", "AEP Texas"],
    "CAISO":      ["California", "CAISO", "CPUC", "PG&E", "SCE", "SDG&E"],
    "TESLA":      ["Tesla", "Megapack", "Powerwall", "Autobidder"],
    "COMPETITOR": ["BYD", "CATL", "Fluence", "Samsung SDI", "Wärtsilä", "Sungrow", "Hithium"],
    "POLICY":     ["IRA", "FEOC", "NFPA", "interconnection", "tariff"],
    "SAFETY":     ["fire", "thermal runaway", "incident", "explosion"],
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def load_sources():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)["sources"]


def clean_html(text: str) -> str:
    if not text:
        return ""
    return unescape(_HTML_TAG_RE.sub("", text)).strip()


def auto_tag(title: str, summary: str) -> list:
    haystack = f"{title} {summary}".lower()
    tags = []
    for tag, keywords in TAG_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in haystack:
                tags.append(tag)
                break
    return tags


def parse_published(entry) -> str:
    for key in ("published", "updated", "created", "pubDate"):
        value = entry.get(key)
        if value:
            return value
    return ""


def to_iso_utc(raw_string: str = "", parsed_struct=None) -> str:
    """Normalize a publication date to an ISO 8601 UTC string. Returns '' if unparseable.

    Prefers feedparser's pre-parsed time.struct_time (already in UTC); falls back to
    parsing the raw RSS string as RFC 2822, then ISO 8601.
    """
    if parsed_struct:
        try:
            ts = calendar.timegm(parsed_struct)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    if raw_string:
        try:
            dt = parsedate_to_datetime(raw_string)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
        try:
            dt = datetime.fromisoformat(raw_string.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except (ValueError, AttributeError):
            pass
    return ""


def backfill_published_at(conn: sqlite3.Connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id, published_date FROM articles WHERE published_at IS NULL OR published_at = ''")
    rows = cur.fetchall()
    updates = [(to_iso_utc(raw_string=raw), row_id) for row_id, raw in rows]
    updates = [u for u in updates if u[0]]
    if updates:
        cur.executemany("UPDATE articles SET published_at = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def url_exists(conn: sqlite3.Connection, url: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM articles WHERE url = ? LIMIT 1", (url,))
    return cur.fetchone() is not None


def insert_article(conn: sqlite3.Connection, article: dict) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO articles
                (title, url, source_name, published_date, published_at, summary, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article["title"],
                article["url"],
                article["source_name"],
                article["published_date"],
                article["published_at"],
                article["summary"],
                json.dumps(article["tags"]),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def scrape_source(conn: sqlite3.Connection, source: dict) -> tuple:
    name = source["name"]
    url = source["url"]
    print(f"  → Fetching {name} …", end=" ", flush=True)

    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"FAILED ({type(e).__name__}: {str(e)[:80]})")
        return 0, 0
    if getattr(feed, "bozo", 0) and not feed.entries:
        print(f"FAILED ({getattr(feed, 'bozo_exception', 'unknown error')})")
        return 0, 0

    added = skipped = 0
    for entry in feed.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        if url_exists(conn, link):
            skipped += 1
            continue

        title = clean_html(entry.get("title", ""))
        summary = clean_html(entry.get("summary") or entry.get("description") or "")
        raw_date = parse_published(entry)
        article = {
            "title": title,
            "url": link,
            "source_name": name,
            "published_date": raw_date,
            "published_at": to_iso_utc(raw_string=raw_date, parsed_struct=entry.get("published_parsed") or entry.get("updated_parsed")),
            "summary": summary,
            "tags": auto_tag(title, summary),
        }
        if insert_article(conn, article):
            added += 1
        else:
            skipped += 1

    print(f"+{added} new, {skipped} skipped")
    return added, skipped


def print_stats(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    bar = "=" * 70

    print(f"\n{bar}\nARTICLES PER SOURCE\n{bar}")
    cur.execute(
        "SELECT source_name, COUNT(*) FROM articles GROUP BY source_name ORDER BY 2 DESC"
    )
    for source, count in cur.fetchall():
        print(f"  {source:<30} {count:>5}")

    print(f"\n{bar}\nARTICLES PER TAG\n{bar}")
    tag_counts = {tag: 0 for tag in TAG_KEYWORDS}
    untagged = 0
    cur.execute("SELECT tags FROM articles")
    for (tags_json,) in cur.fetchall():
        try:
            tags = json.loads(tags_json) if tags_json else []
        except json.JSONDecodeError:
            tags = []
        if not tags:
            untagged += 1
        for t in tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {tag:<15} {count:>5}")
    print(f"  {'(untagged)':<15} {untagged:>5}")

    print(f"\n{bar}\n10 MOST RECENTLY PUBLISHED ARTICLES\n{bar}")
    cur.execute(
        """
        SELECT title, source_name, published_at, published_date, tags
        FROM articles
        WHERE published_at IS NOT NULL AND published_at != ''
        ORDER BY published_at DESC
        LIMIT 10
        """
    )
    for i, (title, source, pub_at, pub_raw, tags_json) in enumerate(cur.fetchall(), 1):
        try:
            tags = json.loads(tags_json) if tags_json else []
        except json.JSONDecodeError:
            tags = []
        tag_str = ", ".join(tags) if tags else "—"
        title_display = title if len(title) <= 95 else title[:92] + "…"
        print(f"\n  {i:>2}. {title_display}")
        print(f"      Source : {source}")
        print(f"      Date   : {pub_at or pub_raw or 'n/a'}")
        print(f"      Tags   : {tag_str}")

    cur.execute("SELECT COUNT(*) FROM articles")
    total = cur.fetchone()[0]
    print(f"\n{bar}\nTOTAL ARTICLES IN DATABASE: {total}\n{bar}")


def main() -> None:
    print(f"BESS Market Intelligence — RSS Scraper")
    print(f"Database: {DB_PATH}\n")

    conn = init_db()
    backfilled = backfill_published_at(conn)
    if backfilled:
        print(f"Backfilled published_at for {backfilled} pre-existing rows\n")

    sources = load_sources()
    print(f"Loaded {len(sources)} RSS sources\n")

    total_added = total_skipped = 0
    for source in sources:
        added, skipped = scrape_source(conn, source)
        total_added += added
        total_skipped += skipped

    print(f"\nRun complete: +{total_added} new, {total_skipped} skipped (duplicates)")
    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
