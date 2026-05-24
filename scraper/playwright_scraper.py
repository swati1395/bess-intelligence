"""
Playwright-based scraper for JavaScript-rendered sites that defeat plain HTTP.

Local-only — requires `pip install playwright` + `playwright install chromium`.
NOT wired into the GitHub Actions daily workflow (the browser binary adds
~150 MB and a setup step that's not worth it for sites the rest of the
pipeline can already cover via wire-service feeds).

Run:
  python3 scraper/playwright_scraper.py --lg            # LG Energy Solution

Add more JS-rendered targets here as needed (Trina parent, Hithium-related).
"""

import argparse
import os
import re
import sys

from bs4 import BeautifulSoup
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from database.setup_db import init_db  # noqa: E402
from scraper.rss_scraper import (  # noqa: E402
    auto_tag, clean_html, insert_article, url_exists, to_iso_utc,
)

try:
    from playwright.sync_api import (
        sync_playwright, TimeoutError as PWTimeoutError,
    )
except ImportError:
    sys.exit(
        "Playwright is not installed.\n"
        "Run: pip install playwright && python3 -m playwright install chromium"
    )


LG_URL = "https://www.lgensol.com/en/company/newsroom"
LG_DETAIL_BASE = "https://www.lgensol.com/en/company/newsroom-detail?seq={}"
LG_KEYWORDS = [
    "battery", "energy storage", "ess", "bess", "lfp",
    "cells", "manufacturing",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
GO_DETAIL_RE = re.compile(r"goDetail\((\d+)\)")


def fetch_rendered_html(url: str, wait_for_selector: str = None,
                        timeout_ms: int = 30000) -> str:
    """Launch headless Chromium, navigate, and return the rendered DOM.

    Waits for `domcontentloaded` (fast), then for `wait_for_selector` to appear
    in the DOM. Avoids `networkidle` because many corporate sites keep firing
    analytics/tracker requests and never go idle."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if wait_for_selector:
                page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
            # Brief settle for any deferred DOM updates after the selector appears
            page.wait_for_timeout(1500)
            return page.content()
        finally:
            browser.close()


def parse_lg_articles(html_text: str) -> list:
    """Pull article records from a rendered LG ES newsroom DOM.
    Each <li.news-item> has: an <img> with onclick goDetail(N) giving the ID,
    <h2.news-title> for the title, span[aria-label="게시일"] for the date,
    and <p.ellipsis3> for a summary."""
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    seen = set()

    for li in soup.find_all(class_="news-item"):
        title_el = li.find(class_="news-title")
        if not title_el:
            continue
        title = clean_html(title_el.get_text(" ", strip=True))
        if not title or len(title) < 8:
            continue

        # Extract article ID from any goDetail() onclick on this li
        seq = None
        for tag_with_click in li.find_all(onclick=GO_DETAIL_RE):
            m = GO_DETAIL_RE.search(tag_with_click.get("onclick", ""))
            if m:
                seq = m.group(1)
                break
        if not seq:
            continue
        url = LG_DETAIL_BASE.format(seq)
        if url in seen:
            continue
        seen.add(url)

        date_el = li.find("span", attrs={"aria-label": "게시일"})
        date_raw = date_el.get_text(" ", strip=True) if date_el else ""

        summary_el = li.find("p", class_=re.compile("ellipsis"))
        summary = clean_html(summary_el.get_text(" ", strip=True)) if summary_el else ""

        items.append({
            "title":          title,
            "url":            url,
            "source_name":    "LG Energy Solution",
            "published_date": date_raw,
            "published_at":   to_iso_utc(raw_string=date_raw),
            "summary":        summary,
            "tags":           auto_tag(title, summary),
            "_seq":           seq,
        })
    return items


def filter_bess_relevant(items: list, keywords: list) -> list:
    """Keep articles whose title OR summary contains any of the keywords
    (case-insensitive substring match). Empty list of keywords = no filter."""
    if not keywords:
        return items
    filtered = []
    for item in items:
        haystack = f"{item['title']} {item.get('summary', '')}".lower()
        if any(kw.lower() in haystack for kw in keywords):
            filtered.append(item)
    return filtered


def scrape_lg() -> int:
    print(f"→ Launching headless Chromium to fetch {LG_URL}")
    try:
        html_text = fetch_rendered_html(LG_URL, wait_for_selector=".news-item")
    except PWTimeoutError as e:
        sys.exit(f"Playwright timeout: {e}")
    print(f"  Got {len(html_text):,} bytes of rendered HTML")

    all_items = parse_lg_articles(html_text)
    print(f"  Parsed {len(all_items)} article(s) total")

    items = filter_bess_relevant(all_items, LG_KEYWORDS)
    print(f"  {len(items)} match BESS keywords {LG_KEYWORDS}")

    conn = init_db()
    added = skipped = 0
    for item in items:
        item_for_db = {k: v for k, v in item.items() if not k.startswith("_")}
        if url_exists(conn, item_for_db["url"]):
            skipped += 1
            continue
        if insert_article(conn, item_for_db):
            added += 1
        else:
            skipped += 1
    conn.commit()
    print(f"  Stored {added} new, skipped {skipped} duplicates\n")

    print("=== Sample articles (up to 5) ===")
    for i, item in enumerate(items[:5], 1):
        print(f"  {i}. [{item['published_date'] or 'no date':<12}] "
              f"{item['title'][:90]}")
        print(f"     {item['url']}")

    conn.close()
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("Run:")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lg", action="store_true",
                        help="Scrape LG Energy Solution newsroom (lgensol.com).")
    args = parser.parse_args()

    if not args.lg:
        parser.print_help()
        sys.exit(1)

    scrape_lg()


if __name__ == "__main__":
    main()
