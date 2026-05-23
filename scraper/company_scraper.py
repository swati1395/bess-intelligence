"""
Company newsroom scraper — Module 6 expansion of Session 6.

Scrapes BESS-relevant company press pages and inserts new articles into the
shared `articles` table (same schema as the RSS scraper). Uses the existing
auto_tag() and insert_article() helpers from scraper.rss_scraper, so dedup
(URL-unique) and keyword tagging work identically.

Reality check on coverage: many corporate newsrooms are either
  - JS-rendered (Tesla blog requires headless browser),
  - Cloudflare-protected (returns 403 to plain Python requests),
  - inaccessible at the URLs in the original spec (Trina's domain expired),
  - or refuse scraping outright (LG ES connection refused).
Scrapers for those sites raise a documented exception; the main loop catches
each failure independently so unreachable sites do not block the working ones.

Run:
  python3 scraper/company_scraper.py                     # all companies
  python3 scraper/company_scraper.py --companies sungrow,byd   # subset
"""

import argparse
import os
import re
import sys
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from database.setup_db import init_db  # noqa: E402
from scraper.rss_scraper import (  # noqa: E402
    auto_tag, clean_html, insert_article, url_exists, to_iso_utc,
)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}
TIMEOUT = 20

TESLA_KEYWORDS = ["megapack", "powerwall", "energy storage", "autobidder", "lathrop"]


def fetch_html(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    r.encoding = r.apparent_encoding or r.encoding or "utf-8"
    # Don't raise_for_status — some sites (Sungrow) return 404 status on valid pages.
    return r.text


def article_dict(title: str, url: str, summary: str = "", date_raw: str = "",
                 date_iso: str = "") -> dict:
    title = clean_html(title)
    summary = clean_html(summary)
    return {
        "title": title,
        "url": url,
        "source_name": None,  # set by caller
        "published_date": date_raw,
        "published_at": date_iso or to_iso_utc(raw_string=date_raw),
        "summary": summary,
        "tags": auto_tag(title, summary),
    }


# === Per-company scrapers ===========================================

def scrape_form_energy() -> list:
    """Form Energy newsroom (WordPress, posts on listing page)."""
    url = "https://formenergy.com/news/"
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()
    for post in soup.find_all(class_=re.compile(r"^post-\d+$")):
        link = post.find("a", href=True)
        if not link:
            continue
        href = urljoin(url, link["href"])
        if href in seen:
            continue
        seen.add(href)
        title_el = post.find(["h1", "h2", "h3", "h4"]) or link
        title = title_el.get_text(" ", strip=True)
        if not title or len(title) < 8:
            title = link.get("title", "") or link.get_text(" ", strip=True)
        if not title or "form energy" in title.lower() and len(title) < 25:
            continue
        items.append(article_dict(title, href))
    return items


def scrape_byd() -> list:
    """BYD English news (substitute URL — original bydenergystorage.com is unreachable).
    The page uses plain anchor tags rather than WordPress-style post containers."""
    url = "https://en.byd.com/news/"
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("https://en.byd.com/news/"):
            continue
        if href.rstrip("/") == "https://en.byd.com/news":
            continue
        if href in seen:
            continue
        seen.add(href)
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 12:
            continue
        items.append(article_dict(title, href))
    return items


def scrape_sungrow() -> list:
    """Sungrow news listing (returns HTTP 404 but page has content)."""
    url = "https://en.sungrowpower.com/newsList"
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/en/") or href == "/en/" or "newsList" in href:
            continue
        slug = href.rstrip("/").split("/")[-1]
        if "-" not in slug or len(slug) < 12:
            continue
        full = urljoin(url, href)
        if full in seen:
            continue
        seen.add(full)
        title = a.get_text(" ", strip=True)
        if not title or title.lower() in ("global news", "news", "read more") or len(title) < 8:
            # Fall back to slug-derived title — title-case the kebab-case slug
            title = slug.replace("-", " ").title()
        items.append(article_dict(title, full))
    return items[:50]


def scrape_tesla() -> list:
    raise RuntimeError(
        "Tesla blog (tesla.com/blog) returns HTTP 403 to plain requests — "
        "Cloudflare bot challenge. Bypassing requires a headless browser "
        "(Playwright/Selenium) which adds heavy CI deps. Recommend pulling "
        "Tesla news via Reuters/Electrek RSS feeds (already in sources.yaml)."
    )


def scrape_fluence() -> list:
    raise RuntimeError(
        "Fluence newsroom URLs (fluenceenergy.com/news, /news-events, "
        "/press-releases) all return 404. Fluence IR RSS "
        "(ir.fluenceenergy.com/rss) was added to sources.yaml in Part 1."
    )


def scrape_hithium() -> list:
    raise RuntimeError(
        "Hithium news pages (hithium.com/news, /en/news) return empty bodies "
        "or 404 — the site appears to be entirely JS-rendered. Would need "
        "Playwright or an undocumented API endpoint."
    )


def scrape_trina() -> list:
    raise RuntimeError(
        "trinastorage.com is dead — domain returns 'Squarespace - Website "
        "Expired'. Trina Storage may have rebranded under the parent Trina "
        "Solar site (trinasolar.com); use that if BESS coverage is needed."
    )


def scrape_lg_es() -> list:
    raise RuntimeError(
        "LG Energy Solution (lgenergysolution.com) refuses connection from "
        "this client — likely IP-based or anti-bot blocking. Their news is "
        "syndicated by Korean industry press and trade outlets."
    )


def scrape_samsung_sdi() -> list:
    raise RuntimeError(
        "Samsung SDI newsroom URL (samsung.com/global/business/sdi/news) "
        "returns 404 at every variant tested. SDI news typically reaches "
        "English-language readers via Reuters/Korea Times rather than direct."
    )


# === Source registry ================================================

# slug -> (display_name, scraper_function)
SOURCES = {
    "form_energy":   ("Form Energy",  scrape_form_energy),
    "byd":           ("BYD",          scrape_byd),
    "sungrow":       ("Sungrow",      scrape_sungrow),
    "tesla":         ("Tesla",        scrape_tesla),
    "fluence":       ("Fluence",      scrape_fluence),
    "hithium":       ("Hithium",      scrape_hithium),
    "trina":         ("Trina",        scrape_trina),
    "lg_es":         ("LG ES",        scrape_lg_es),
    "samsung_sdi":   ("Samsung SDI",  scrape_samsung_sdi),
}


def apply_tesla_filter(items: list) -> list:
    """Tesla scraper is currently a stub, but if it ever yields items, filter
    to BESS-relevant titles only (Megapack / Powerwall / energy storage /
    Autobidder / Lathrop)."""
    return [
        it for it in items
        if any(kw in it["title"].lower() for kw in TESLA_KEYWORDS)
    ]


def run_one(slug: str, conn) -> dict:
    name, scraper = SOURCES[slug]
    print(f"  → {name:<14}", end=" ", flush=True)
    try:
        items = scraper()
        if slug == "tesla":
            items = apply_tesla_filter(items)
        added = skipped = 0
        for it in items:
            it["source_name"] = name
            if url_exists(conn, it["url"]):
                skipped += 1
                continue
            if insert_article(conn, it):
                added += 1
            else:
                skipped += 1
        conn.commit()
        print(f"OK   — {len(items):>3} found, +{added} new, {skipped} dup/skip")
        return {"slug": slug, "name": name, "status": "ok",
                "found": len(items), "added": added, "error": None}
    except Exception as e:
        msg = str(e).split(":")[0] if isinstance(e, requests.RequestException) else str(e)
        print(f"FAIL — {msg[:90]}")
        return {"slug": slug, "name": name, "status": "fail",
                "found": 0, "added": 0, "error": msg}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--companies", default=None,
                        help="Comma-separated subset of company slugs to run "
                             "(default: all). Valid slugs: "
                             + ", ".join(SOURCES))
    args = parser.parse_args()

    requested = [s.strip() for s in args.companies.split(",")] if args.companies else list(SOURCES)
    unknown = [s for s in requested if s not in SOURCES]
    if unknown:
        sys.exit(f"Unknown company slug(s): {unknown}\nValid: {list(SOURCES)}")

    conn = init_db()
    print(f"Running {len(requested)} company scraper(s):\n")
    results = [run_one(slug, conn) for slug in requested]

    bar = "=" * 72
    print(f"\n{bar}\nPER-COMPANY SUMMARY\n{bar}")
    print(f"  {'Status':<6} {'Source':<14} {'Found':>6} {'Added':>6}  Notes")
    print(f"  {'-' * 70}")
    total_added = 0
    for r in results:
        note = "" if not r["error"] else f"  ({r['error'][:50]})"
        status_mark = "OK" if r["status"] == "ok" else "FAIL"
        print(f"  {status_mark:<6} {r['name']:<14} {r['found']:>6} {r['added']:>6}{note}")
        total_added += r["added"]
    print(f"  {'-' * 70}")
    print(f"  Total new articles inserted: {total_added}")

    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_fail = sum(1 for r in results if r["status"] == "fail")
    print(f"\n  Sources working: {n_ok}/{len(results)} "
          f"({n_fail} failed — see notes above and module docstring)")

    conn.close()


if __name__ == "__main__":
    main()
