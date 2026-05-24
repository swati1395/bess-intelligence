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
from datetime import datetime, timezone
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


_TZ_SUFFIX_RE = re.compile(r"\s+\b(ET|EDT|EST|GMT|UTC|PST|PDT|CT|CST|CDT|PT)\b\s*$", re.I)


def parse_loose_date(s: str) -> str:
    """Parse common human-readable date formats into ISO 8601 UTC.
    Examples handled: 'August 11, 2025 16:01 ET', '2022.08.16', 'Aug 11, 2025'.
    Returns '' if unparseable; caller can fall back to raw string."""
    if not s:
        return ""
    s_clean = _TZ_SUFFIX_RE.sub("", s.strip())
    for fmt in ("%B %d, %Y %H:%M", "%B %d, %Y", "%b %d, %Y",
                "%Y-%m-%d", "%Y.%m.%d", "%d %B %Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(s_clean, fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return to_iso_utc(raw_string=s)


def scrape_globenewswire_list(url: str) -> list:
    """Shared scraper for GlobeNewswire search results (.newsLink pattern).
    Used by both Fluence (tag search) and Tesla (organization search)."""
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()
    for nl in soup.find_all(class_="newsLink"):
        main = nl.find(class_="mainLink")
        if not main:
            continue
        link = main.find("a", href=True)
        if not link:
            continue
        href = urljoin(url, link["href"])
        if href in seen:
            continue
        seen.add(href)
        title = link.get_text(" ", strip=True)
        if not title or len(title) < 8:
            continue
        date_raw = ""
        ds = nl.find(class_="date-source")
        if ds:
            first_span = ds.find("span")
            if first_span:
                date_raw = first_span.get_text(" ", strip=True)
        items.append(article_dict(title, href, date_raw=date_raw,
                                  date_iso=parse_loose_date(date_raw)))
    return items


# === Per-company scrapers ===========================================

_FORM_ENERGY_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d+,?\s+\d{4}"
)


def scrape_form_energy() -> list:
    """Form Energy newsroom (WordPress + Elementor). Two post types co-exist:
      - 'post':          Form Energy's own announcements (URLs on formenergy.com)
      - 'press_article': External coverage (URLs on Fast Company / TechCrunch / etc.)
    Both are useful competitive signals; both stored under source_name 'Form Energy'.

    Date extraction: there's no <time> tag — the date is rendered as inline text
    in 'Month D, YYYY' format, typically immediately before the title. Regex-match
    on the post container's full text.

    URL extraction: the FIRST anchor in each post container is often an image
    wrapper or a tag link, not the title. Find the anchor with substantive text
    (>20 chars, not 'Read More') instead."""
    url = "https://formenergy.com/news/"
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()

    for post in soup.find_all(class_=re.compile(r"^post-\d+$")):
        # Find the title anchor: skip empty/image links, skip "Read More" buttons,
        # skip short tag links (Fast Company, TechCrunch, etc. appear with their
        # publication name as link text — those are tag pages, not titles).
        title = href = None
        for a in post.find_all("a", href=True):
            text = a.get_text(" ", strip=True)
            if not text or len(text) < 20:
                continue
            if text.lower() in ("read more",):
                continue
            title, href = text, a["href"]
            break
        if not title or not href:
            continue
        # Strip Elementor-style new-tab fragments (#new_tab, # at end)
        href = re.sub(r"#new_tab.*$", "", href).rstrip("#")
        href = urljoin(url, href)
        if href in seen:
            continue
        seen.add(href)

        # Date: regex the post's full text for the first 'Month D, YYYY'
        m = _FORM_ENERGY_DATE_RE.search(post.get_text(" ", strip=True))
        date_raw = m.group(0) if m else ""

        items.append(article_dict(title, href, date_raw=date_raw,
                                  date_iso=parse_loose_date(date_raw)))
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
    """Tesla news via GlobeNewswire tag search. Cloudflare blocks tesla.com/blog
    directly. Note: /search/organization/Tesla returns 0 results — Tesla doesn't
    have an org profile on GNW under that name. /search/tag/tesla works (includes
    third-party press releases mentioning Tesla). Results are then filtered
    downstream to TESLA_KEYWORDS (Megapack/Powerwall/energy storage/Autobidder/Lathrop)."""
    return scrape_globenewswire_list(
        "https://www.globenewswire.com/search/tag/tesla"
    )


def scrape_fluence() -> list:
    """Fluence press releases via GlobeNewswire tag search. The
    fluenceenergy.com/news URL no longer exists; GlobeNewswire aggregates
    their wire releases plus a few third-party releases mentioning Fluence
    (analyst notes, class-action filings) that get filtered by keyword
    tagging downstream."""
    return scrape_globenewswire_list(
        "https://www.globenewswire.com/search/tag/fluence"
    )


def scrape_hithium() -> list:
    """Hithium newsroom — uses /newsroom/LatestUpdates.html as the article listing.
    Articles are <h1> tags nested inside parent <a> wrappers, each linking to
    /newsroom/latest/details/<id>.html."""
    url = "https://www.hithium.com/newsroom/LatestUpdates.html"
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()
    for h1 in soup.find_all("h1"):
        link = h1.find_parent("a", href=True) or h1.find("a", href=True)
        if not link:
            continue
        href = urljoin(url, link["href"])
        if "/details/" not in href:
            continue
        if href in seen:
            continue
        seen.add(href)
        title = h1.get_text(" ", strip=True)
        if not title or len(title) < 8:
            continue
        items.append(article_dict(title, href))
    return items


def scrape_trina() -> list:
    """Trina parent-company press release page. Original trinastorage.com is
    a dead domain; trinasolar.com is the parent. Note: the press-releases page
    loads its actual list via JavaScript — this scraper picks up whatever
    article links are present in the initial HTML (often 0)."""
    url = "https://www.trinasolar.com/eu/press-releases/"
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()
    # Press release URLs are long slugged paths under trinasolar.com (not nav)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        # Heuristic: a press release URL has a long kebab-case slug as last segment
        last = href.rstrip("/").split("/")[-1]
        slug_len = len(last.replace("-", ""))
        if slug_len < 25 or " " in last:
            continue
        # Must be on trinasolar.com domain or relative
        if href.startswith("http") and "trinasolar.com" not in href:
            continue
        full = urljoin(url, href)
        if full in seen:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 15:
            continue
        seen.add(full)
        items.append(article_dict(title, full))
    return items


def scrape_lg_es() -> list:
    """LG Energy Solution newsroom on the lgensol.com domain. Note: the news
    list (<ul class='news-list'>) is populated by JavaScript at runtime —
    plain HTTP returns an empty <ul>. We scan all anchors as a best effort;
    expect 0 articles unless LG ES adds SSR or a JSON endpoint we can find."""
    url = "https://www.lgensol.com/en/company/newsroom"
    soup = BeautifulSoup(fetch_html(url), "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Looking for individual news article URLs
        if not re.search(r"(news|article|press|release)/\w+", href, re.I):
            continue
        if href.rstrip("/").endswith("newsroom"):
            continue
        full = urljoin(url, href)
        if full in seen:
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 12:
            continue
        seen.add(full)
        items.append(article_dict(title, full))
    return items


FLUENCE_BLOG_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d+,?\s+\d{4}"
)


def scrape_fluence_blog() -> list:
    """Fluence Blog at blog.fluenceenergy.com — HubSpot CMS, server-rendered.
    Paginates /page/2..5. Each post is an <a> wrapper containing an <h3> for
    the title and an inline span with the date in 'Month D, YYYY' format."""
    base = "https://blog.fluenceenergy.com"
    items, seen = [], set()
    for page in [1, 2, 3, 4, 5]:
        url = base if page == 1 else f"{base}/page/{page}"
        try:
            soup = BeautifulSoup(fetch_html(url), "html.parser")
        except requests.RequestException:
            continue
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("https://blog.fluenceenergy.com/"):
                continue
            slug = href.rstrip("/").split("/")[-1]
            if len(slug) < 15 or slug == "page" or slug.isdigit():
                continue
            h3 = a.find("h3")
            if not h3:
                continue
            if href in seen:
                continue
            seen.add(href)
            title = h3.get_text(" ", strip=True)
            if not title or len(title) < 8:
                continue
            m = FLUENCE_BLOG_DATE_RE.search(a.get_text(" ", strip=True))
            date_raw = m.group(0) if m else ""
            items.append(article_dict(title, href, date_raw=date_raw,
                                      date_iso=parse_loose_date(date_raw)))
    return items


TESLA_IR_KEYWORDS = ["energy storage", "gwh", "megapack", "powerwall",
                     "autobidder", "deployment"]


def scrape_tesla_ir() -> list:
    """Tesla IR Press at ir.tesla.com/press — Drupal CMS, paginates ?page=0,1.
    Filters titles to BESS-relevant content only.

    Reality check: this URL returns HTTP 403 to plain Python requests due to
    Cloudflare bot management (__cf_bm cookie requires a JS-capable client to
    pass the challenge). The scraper attempts the request and reports clean
    failure when blocked; if Cloudflare ever softens or the request comes from
    a whitelisted CI runner IP, it will start yielding results automatically.
    For reliable Tesla BESS coverage in the meantime, the GlobeNewswire-based
    Tesla scraper (scrape_tesla) and the Electrek/Reuters RSS feeds are the
    productive paths."""
    base = "https://ir.tesla.com/press"
    items, seen = [], set()
    any_success = False
    last_status = None
    for page in [0, 1]:
        url = f"{base}?page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                             allow_redirects=True)
            last_status = r.status_code
            if r.status_code != 200:
                continue
        except requests.RequestException:
            continue
        any_success = True
        soup = BeautifulSoup(r.text, "html.parser")
        # Drupal views typically use h2/h3 inside views-row containers
        for h in soup.find_all(["h2", "h3", "h4"]):
            link = h.find("a", href=True) or h.find_parent("a", href=True)
            if not link:
                continue
            href = urljoin(base, link["href"])
            title = h.get_text(" ", strip=True)
            if not title or len(title) < 12:
                continue
            tlow = title.lower()
            if not any(kw in tlow for kw in TESLA_IR_KEYWORDS):
                continue
            if href in seen:
                continue
            seen.add(href)
            items.append(article_dict(title, href))
    if not any_success:
        raise RuntimeError(
            f"ir.tesla.com/press returned HTTP {last_status} (Cloudflare bot "
            f"challenge). Plain Python requests cannot pass the JS-cookie "
            f"check. Bypass requires Playwright/Selenium with a real browser."
        )
    return items


def scrape_samsung_sdi() -> list:
    """Samsung SDI news at /sdi-now/sdi-news/list.html, paginated via
    ?pageIndex=1..5. Every <li> in news_list shares the same href
    (news_view.html — they wire up onclick handlers via JS), so we synthesize
    unique URLs from each title slug for our dedup to work."""
    base = "https://www.samsungsdi.com/sdi-now/sdi-news/list.html"
    items, seen = [], set()
    for page in range(1, 6):
        url = f"{base}?pageIndex={page}"
        try:
            soup = BeautifulSoup(fetch_html(url), "html.parser")
        except requests.RequestException:
            continue
        news_list = soup.find(class_="news_list")
        if not news_list:
            continue
        for li in news_list.find_all("li"):
            tit_el = li.find(class_="tit")
            date_el = li.find(class_="date")
            if not tit_el:
                continue
            title = tit_el.get_text(" ", strip=True)
            if not title or len(title) < 8:
                continue
            date_raw = date_el.get_text(" ", strip=True) if date_el else ""
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
            synth_url = (f"https://www.samsungsdi.com/sdi-now/sdi-news/"
                         f"news_view.html#{slug}")
            if synth_url in seen:
                continue
            seen.add(synth_url)
            items.append(article_dict(title, synth_url, date_raw=date_raw,
                                      date_iso=parse_loose_date(date_raw)))
    return items


# === Source registry ================================================

# slug -> (display_name, scraper_function)
SOURCES = {
    "form_energy":   ("Form Energy",  scrape_form_energy),
    "byd":           ("BYD",          scrape_byd),
    "sungrow":       ("Sungrow",      scrape_sungrow),
    "tesla":         ("Tesla",        scrape_tesla),
    "tesla_ir":      ("Tesla IR",     scrape_tesla_ir),
    "fluence":       ("Fluence",      scrape_fluence),
    "fluence_blog":  ("Fluence Blog", scrape_fluence_blog),
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
