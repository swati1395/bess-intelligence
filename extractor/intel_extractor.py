import argparse
import os
import sqlite3
import sys
import time
from typing import Literal, Optional

import litellm                # Gemini via the unified LiteLLM interface
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from database.setup_db import DB_PATH, init_db  # noqa: E402

# Quiet, resilient LiteLLM (same settings the eval validated).
litellm.drop_params = True            # drop reasoning_effort silently if a provider rejects it
litellm.suppress_debug_info = True
os.environ.setdefault("LITELLM_LOG", "ERROR")

PRIMARY_MODEL = "gemini/gemini-3.1-flash-lite"
BATCH_SIZE = 10
MAX_SUMMARY_CHARS = 4000
MAX_TOKENS = 2048                  # raised from 1024 so Gemini reasoning can't truncate the JSON
GEMINI_RETRIES = 2                 # short retries before giving up on an article
GEMINI_RETRY_DELAY = 4             # seconds, fixed
GEMINI_TIMEOUT = 30                # hard per-call timeout so a stuck request can't hang the cron

# Gemini failures that warrant a retry (the 503 family we observed, plus timeouts).
GEMINI_TRANSIENT = (
    litellm.ServiceUnavailableError,
    litellm.RateLimitError,
    litellm.Timeout,
    litellm.InternalServerError,
    litellm.APIConnectionError,
)

EventType = Literal[
    "deployment",
    "contract_announcement",
    "product_launch",
    "financial_results",
    "regulatory_change",
    "safety_incident",
    "partnership",
    "merger_acquisition",
    "analysis_commentary",
    "other",
]
IsoMarket = Literal["ERCOT", "CAISO", "PJM", "NYISO", "MISO", "SPP", "ISO-NE"]
Category = Literal[
    "Deployment & Projects",
    "Supply Chain & Manufacturing",
    "Technology",
    "Policy & Regulation",
    "Market & Commercial",
    "Applications & Adjacent",
]


class ExtractedIntel(BaseModel):
    company: Optional[str] = Field(
        None,
        description=(
            "Primary entity that is the subject of the news (e.g. 'Tesla', 'Fluence', "
            "'CPS Energy'). null if the article is not about a single identifiable company."
        ),
    )
    event_type: Optional[EventType] = Field(
        None,
        description="Canonical event category. null only if the article is not news.",
    )
    product_name: Optional[str] = Field(
        None,
        description="Specific named product if mentioned (e.g. 'Megapack 2 XL'). null otherwise.",
    )
    capacity_mwh: Optional[float] = Field(
        None,
        description=(
            "Energy capacity in megawatt-hours. If the article gives MW and duration, "
            "compute MWh = MW × hours. If only MW (no duration) is given, return null. "
            "For GWh multiply by 1000."
        ),
    )
    location_state: Optional[str] = Field(
        None,
        description="Two-letter US state abbreviation (e.g. 'TX'). null for non-US, multi-state, or pure analysis pieces.",
    )
    iso_market: Optional[IsoMarket] = Field(
        None,
        description="Wholesale market operator. Infer from state when unambiguous (TX→ERCOT, most CA→CAISO).",
    )
    customer: Optional[str] = Field(
        None,
        description="Buyer/offtaker named in a sales or contract context (e.g. 'Vistra'). null otherwise.",
    )
    significance_score: int = Field(
        ...,
        ge=1,
        le=5,
        description=(
            "1-5 importance rating for BESS competitive intelligence. "
            "5=Critical (market-moving event, major product launch, $500M+ deal, policy shift). "
            "4=High (contract wins, M&A, notable competitor moves). "
            "3=Medium (industry analysis, technology updates, smaller deals). "
            "2=Low (adjacent market signals, EV supply chain, solar, grid infrastructure). "
            "1=Monitoring (general energy news, low BESS specificity)."
        ),
    )
    significance_reason: str = Field(
        ...,
        description="One sentence justifying the score in BESS-competitive terms.",
    )
    brief: Optional[str] = Field(
        None,
        description=(
            "One sentence in Bloomberg terminal style: named actor + active verb + key number "
            "($, MWh, GWh, MW, %) if present + one-clause so-what. "
            "Vary sentence openings — no fixed template. "
            "Banned: landmark, market-moving, significant, notable, major. "
            "If pure analysis with no named actor, open with the finding. "
            "Target 20–35 words; never exceed 40."
        ),
    )
    category: Category = Field(
        ...,
        description=(
            "Single best-fit TOPIC category (one axis; event_type is the separate "
            "kind-of-news axis). Pick exactly one:\n"
            "- Deployment & Projects: grid-scale BESS projects — announcements, approvals, financing, construction, commissioning.\n"
            "- Supply Chain & Manufacturing: cells, factories, raw materials, sourcing, FEOC, tariffs, trade restrictions.\n"
            "- Technology: grid-forming, chemistries (LFP, sodium-ion, solid-state), long-duration, software, safety.\n"
            "- Policy & Regulation: government rules, ITC, mandates, interconnection rules, grid-fee structures, ISO market-design rulings.\n"
            "- Market & Commercial: pricing, revenue, financing trends, M&A, financial results, capital raises, market sizing, strategy analysis.\n"
            "- Applications & Adjacent: data centers, co-location, residential/balcony batteries, C&I, EV-adjacent, battery-adjacent PR/HR news.\n"
            "If an article fits two topics, apply the tie-breaker priority (highest wins): "
            "1 Policy & Regulation, 2 Supply Chain & Manufacturing, 3 Deployment & Projects, "
            "4 Market & Commercial, 5 Technology, 6 Applications & Adjacent.\n"
            "Note: M&A is NOT a category — classify M&A stories under Market & Commercial "
            "and set event_type=merger_acquisition."
        ),
    )


SYSTEM_PROMPT = """If the article title or summary is in Chinese, Japanese, Korean, or any non-English language, translate it to English first before extracting any fields. Then proceed with extraction as normal.

You are a competitive intelligence analyst for the Battery Energy Storage System (BESS) industry, with deep focus on the ERCOT (Texas) and CAISO (California) wholesale electricity markets.

Your task: read a news article title and summary, then extract structured intelligence fields conforming to the provided schema. Return ONLY the JSON object — no prose, no preamble, no explanation outside the schema fields.

## Extraction principles

- Be conservative with non-null values. If a field is not explicitly supported by the text, return null. Do not invent companies, capacities, or customers.
- Prefer the company's most commonly recognized name ("Tesla", not "Tesla, Inc."; "PG&E", not "Pacific Gas and Electric Company").
- For `capacity_mwh`: ONLY report MWh when the article makes the energy figure unambiguous. "100 MW / 400 MWh battery" → 400. "100 MW battery" alone → null (no duration). "5 GWh project" → 5000.
- For `iso_market`: only fill in when the article clearly involves grid services, market participation, or a project sited in that ISO's footprint. Do not infer ERCOT/CAISO from a passing state mention in an EV story.
- For `customer`: this is the BUYER in a transaction (utility, IPP, hyperscaler offtaker), not the SELLER. If unclear, return null.

## Significance scoring rubric (1-5)

- **5 — Critical**: market-moving event, major product launch, $500M+ deal, policy shift.
- **4 — High**: contract wins, M&A, notable competitor moves.
- **3 — Medium**: industry analysis, technology updates, smaller deals.
- **2 — Low**: adjacent market signals, EV supply chain, solar, grid infrastructure.
- **1 — Monitoring**: general energy news, low BESS specificity.

Be honest about low scores — many feed items will be 1s or 2s and that is fine.

## Category classification

Category is the TOPIC axis — what the story is *about*. It is independent of `event_type`,
which captures the *kind* of news (deployment, contract, merger_acquisition, etc.). Choosing a
category does not constrain event_type, and vice versa.

Every article gets exactly one category. The six categories are mutually exclusive:

- **Deployment & Projects**: grid-scale BESS projects — announcements, approvals, financing, construction, commissioning of specific storage projects.
- **Supply Chain & Manufacturing**: cells, factories, raw materials, sourcing, FEOC, tariffs, trade restrictions — anything upstream of the deployed system.
- **Technology**: grid-forming inverters, chemistries (LFP, sodium-ion, solid-state), long-duration storage, software, safety/fire research.
- **Policy & Regulation**: government rules, ITC, mandates, interconnection rules, grid-fee structures, ISO/FERC/PUC market-design rulings.
- **Market & Commercial**: pricing, revenue, financing trends, M&A, financial results, capital raises, market sizing, strategy/analyst commentary.
- **Applications & Adjacent**: data centers, co-location, residential/balcony batteries, C&I, EV-adjacent, and battery-adjacent PR/HR news.

**Tie-breaker priority** — when an article genuinely fits two topics, pick the highest on this list:

1. Policy & Regulation
2. Supply Chain & Manufacturing
3. Deployment & Projects
4. Market & Commercial
5. Technology
6. Applications & Adjacent

**M&A is not a category.** Mergers, acquisitions, and funding rounds belong to **Market & Commercial**,
with `event_type` set to `merger_acquisition`. Keep the topic (category) and the deal-shape (event_type) separate.

## News brief

The `brief` field is ONE sentence in Bloomberg terminal style:
- Open with the named actor (company, agency, country) as subject + active verb.
- Embed the key number ($, MWh, GWh, MW, %) if the article contains one.
- Close with one clause on the commercial or strategic consequence.
- Vary sentence openings — do NOT follow a fixed template across articles.
- Banned words: landmark, market-moving, significant, notable, major.
- Pure analysis with no named actor: open with the finding instead.
- Target 20–35 words; never exceed 40.

Good examples:
  "Tesla signed a $5B deal with NatPower to supply 25 GWh of Megapack systems across Italy and the UK, its largest storage contract to date."
  "Fluence added a 10 MWh high-density Smartstack tier to its platform, tightening its positioning against Tesla's Megapack in the utility-scale segment."
  "Australia awarded 4.2 GW / 16.1 GWh of contracts under its Capacity Investment Scheme, the country's largest coordinated grid-storage procurement."
  "U.S. battery storage additions hit 3.3 GW in Q1 2026, a 54% year-over-year jump driven by developers racing to complete projects ahead of tariff uncertainty."

## Output

Return a single JSON object matching the schema. Required: `significance_score`, `significance_reason`, `brief`, `category`. All other fields may be null."""


TAG_PRIORITY_SQL = """
    CASE
        WHEN tags LIKE '%"ERCOT"%' OR tags LIKE '%"CAISO"%' THEN 3
        WHEN tags LIKE '%"COMPETITOR"%' OR tags LIKE '%"TESLA"%' THEN 2
        WHEN tags LIKE '%"POLICY"%'   OR tags LIKE '%"SAFETY"%' THEN 1
        ELSE 0
    END
"""


def fetch_unprocessed(
    conn: sqlite3.Connection,
    limit: Optional[int] = None,
    prioritize_tags: bool = False,
) -> list:
    cur = conn.cursor()
    order_clause = (
        f"({TAG_PRIORITY_SQL}) DESC, a.published_at DESC, a.id DESC"
        if prioritize_tags
        else "a.published_at DESC, a.id DESC"
    )
    sql = f"""
        SELECT a.id, a.title, a.source_name, a.summary
        FROM articles a
        LEFT JOIN extracted_intel e ON e.article_id = a.id
        WHERE e.id IS NULL
        ORDER BY {order_clause}
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    return cur.fetchall()


def _extract_gemini(title: str, summary: str) -> ExtractedIntel:
    """Extract via gemini-3.1-flash-lite. Raises on transient error or invalid JSON."""
    user_content = f"TITLE: {title}\n\nSUMMARY: {summary}".strip()
    resp = litellm.completion(
        model=PRIMARY_MODEL,
        max_tokens=MAX_TOKENS,
        timeout=GEMINI_TIMEOUT,
        reasoning_effort="low",                 # keep thinking minimal so it doesn't starve the JSON
        response_format=ExtractedIntel,         # unified structured output across providers
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return ExtractedIntel.model_validate_json(resp.choices[0].message.content)


def extract_one(title: str, summary: str) -> tuple:
    """Try Gemini up to GEMINI_RETRIES + 1 times on transient error or invalid JSON.
    After retries are exhausted, raises the last exception — caller marks the article failed.
    Returns (ExtractedIntel, model_used).
    """
    summary = (summary or "")[:MAX_SUMMARY_CHARS]
    last_err = None
    for attempt in range(GEMINI_RETRIES + 1):
        try:
            intel = _extract_gemini(title, summary)
            return intel, PRIMARY_MODEL
        except (GEMINI_TRANSIENT, ValidationError) as e:
            last_err = e
            if attempt < GEMINI_RETRIES:
                print(f"      ~ {PRIMARY_MODEL} {type(e).__name__}; retry "
                      f"{attempt + 1}/{GEMINI_RETRIES} in {GEMINI_RETRY_DELAY}s")
                time.sleep(GEMINI_RETRY_DELAY)
        except Exception:
            raise  # unexpected — surface immediately
    raise last_err


def insert_extraction(conn: sqlite3.Connection, article_id: int, intel: ExtractedIntel,
                      model_used: str) -> None:
    conn.execute(
        """
        INSERT INTO extracted_intel
            (article_id, company, event_type, product_name, capacity_mwh,
             location_state, iso_market, customer, significance_score,
             significance_reason, brief, category, model_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            intel.company,
            intel.event_type,
            intel.product_name,
            intel.capacity_mwh,
            intel.location_state,
            intel.iso_market,
            intel.customer,
            intel.significance_score,
            intel.significance_reason,
            intel.brief,
            intel.category,
            model_used,
        ),
    )


def reextract_recent(conn: sqlite3.Connection, n: int) -> int:
    """Delete extractions for the N most-recently-published articles that
    have been extracted, so they get re-processed on the next pass."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ei.article_id
        FROM extracted_intel ei
        JOIN articles a ON a.id = ei.article_id
        ORDER BY COALESCE(NULLIF(a.published_at, ''), a.published_date) DESC,
                 a.id DESC
        LIMIT ?
        """,
        (n,),
    )
    article_ids = [r[0] for r in cur.fetchall()]
    if not article_ids:
        return 0
    placeholders = ",".join("?" * len(article_ids))
    cur.execute(
        f"DELETE FROM extracted_intel WHERE article_id IN ({placeholders})",
        article_ids,
    )
    deleted = cur.rowcount
    conn.commit()
    return deleted


def format_field(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract structured BESS intelligence from articles.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max number of unprocessed articles to process (default: all).")
    parser.add_argument("--prioritize-tags", action="store_true",
                        help="Process BESS-tagged articles first (ERCOT/CAISO > COMPETITOR/TESLA > POLICY/SAFETY > untagged).")
    parser.add_argument("--reextract", type=int, default=None, metavar="N",
                        help="Delete extractions for the N most-recently-published already-extracted "
                             "articles, then re-run extraction on them (useful after schema changes).")
    args = parser.parse_args()

    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        sys.exit("Error: GEMINI_API_KEY is not set.")

    conn = init_db()

    if args.reextract:
        cleared = reextract_recent(conn, args.reextract)
        print(f"Cleared {cleared} existing extraction(s) for re-processing.\n")

    articles = fetch_unprocessed(conn, limit=args.limit, prioritize_tags=args.prioritize_tags)
    print(f"BESS Intel Extractor — model: {PRIMARY_MODEL}")
    print(f"DB: {DB_PATH}")
    print(f"Tag prioritization: {'ON' if args.prioritize_tags else 'off'}")
    print(f"Unprocessed articles to extract: {len(articles)}\n")

    if not articles:
        print("Nothing to do — all articles already extracted.")
        conn.close()
        return

    results = []
    failed = 0
    model_counts = {}

    for i, (article_id, title, source, summary) in enumerate(articles, 1):
        try:
            intel, model_used = extract_one(title, summary or "")
            insert_extraction(conn, article_id, intel, model_used)
            results.append((article_id, title, source, intel))
            model_counts[model_used] = model_counts.get(model_used, 0) + 1

            tag = f"[{intel.significance_score}] {format_field(intel.company)[:24]:<24}"
            print(f"  {i:>3}/{len(articles)}  {tag}  {title[:60]}")
        except Exception as e:
            failed += 1
            print(f"  {i:>3}/{len(articles)}  FAILED: {title[:50]} — {type(e).__name__}: {e}")

        if i % BATCH_SIZE == 0:
            conn.commit()
            print(f"      ─── committed batch ({i} processed) ───")

    conn.commit()

    bar = "=" * 90
    print(f"\n{bar}")
    print(f"DONE — {len(results)} extracted, {failed} failed")
    split = ", ".join(f"{m}: {n}" for m, n in sorted(model_counts.items()))
    print(f"Model split — {split or '(none)'}")
    print(bar)

    print(f"\n{bar}\nEXTRACTION RESULTS (first 10)\n{bar}")
    for i, (aid, title, source, intel) in enumerate(results[:10], 1):
        print(f"\n  {i:>2}. {title}")
        print(f"      Source            : {source}")
        print(f"      Company           : {format_field(intel.company)}")
        print(f"      Event type        : {format_field(intel.event_type)}")
        print(f"      Product           : {format_field(intel.product_name)}")
        print(f"      Capacity (MWh)    : {format_field(intel.capacity_mwh)}")
        print(f"      Location (state)  : {format_field(intel.location_state)}")
        print(f"      ISO market        : {format_field(intel.iso_market)}")
        print(f"      Customer          : {format_field(intel.customer)}")
        print(f"      Significance      : {intel.significance_score}/5 — {intel.significance_reason}")
    print(f"\n{bar}")

    conn.close()


if __name__ == "__main__":
    main()
