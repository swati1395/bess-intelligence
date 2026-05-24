import argparse
import os
import sqlite3
import sys
from typing import Literal, Optional

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from database.setup_db import DB_PATH, init_db  # noqa: E402

MODEL = "claude-opus-4-7"
BATCH_SIZE = 10
MAX_SUMMARY_CHARS = 4000

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
            "1-5 importance rating for BESS competitive intelligence in ERCOT/CAISO markets. "
            "5=market-moving event (large ERCOT/CAISO deployment, major Tesla product, big regulatory shift, M&A). "
            "4=meaningful competitive signal (notable non-ERCOT/CAISO deployment, competitor product, relevant policy). "
            "3=moderately relevant (adjacent BESS news, smaller deployments). "
            "2=tangential (solar/EV with passing storage mention). "
            "1=barely related (pure EV/solar/grid news with no BESS relevance)."
        ),
    )
    significance_reason: str = Field(
        ...,
        description="One sentence justifying the score in BESS-competitive terms.",
    )


SYSTEM_PROMPT = """You are a competitive intelligence analyst for the Battery Energy Storage System (BESS) industry, with deep focus on the ERCOT (Texas) and CAISO (California) wholesale electricity markets.

Your task: read a news article title and summary, then extract structured intelligence fields conforming to the provided schema. Return ONLY the JSON object — no prose, no preamble, no explanation outside the schema fields.

## Extraction principles

- Be conservative with non-null values. If a field is not explicitly supported by the text, return null. Do not invent companies, capacities, or customers.
- Prefer the company's most commonly recognized name ("Tesla", not "Tesla, Inc."; "PG&E", not "Pacific Gas and Electric Company").
- For `capacity_mwh`: ONLY report MWh when the article makes the energy figure unambiguous. "100 MW / 400 MWh battery" → 400. "100 MW battery" alone → null (no duration). "5 GWh project" → 5000.
- For `iso_market`: only fill in when the article clearly involves grid services, market participation, or a project sited in that ISO's footprint. Do not infer ERCOT/CAISO from a passing state mention in an EV story.
- For `customer`: this is the BUYER in a transaction (utility, IPP, hyperscaler offtaker), not the SELLER. If unclear, return null.

## Significance scoring rubric (1-5)

Score for relevance to a BESS competitive-intelligence audience focused on ERCOT and CAISO:

- **5** — Major competitive/market-moving event. Examples: a >500 MWh ERCOT or CAISO BESS commissioning; a Tesla Megapack product update; a major FERC/CPUC/PUCT order materially changing storage economics; large M&A involving a top-5 OEM or developer.
- **4** — Meaningful competitive signal. Examples: a sizable BESS deployment outside ERCOT/CAISO; a competitor OEM (BYD, CATL, Fluence, Sungrow, Wärtsilä, Hithium, Samsung SDI) product launch or order; relevant federal policy (IRA, FEOC, tariffs); a notable safety incident at a utility-scale BESS site.
- **3** — Moderately relevant. Smaller utility-scale projects, partnership announcements, secondary-market policy, BESS financing/funding rounds.
- **2** — Tangential. Solar or EV stories that mention storage in passing, distant geographies with no clear ERCOT/CAISO read-across.
- **1** — Barely related. Pure EV consumer news, grid/transmission stories with no storage angle, rooftop solar product news.

Be honest about low scores — many feed items will be 1s or 2s and that is fine.

## Output

Return a single JSON object matching the schema. Required fields: `significance_score`, `significance_reason`. All other fields may be null."""


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


def extract_one(client: anthropic.Anthropic, title: str, summary: str):
    summary = (summary or "")[:MAX_SUMMARY_CHARS]
    user_content = f"TITLE: {title}\n\nSUMMARY: {summary}".strip()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
        output_format=ExtractedIntel,
    )
    return response.parsed_output, response.usage


def insert_extraction(conn: sqlite3.Connection, article_id: int, intel: ExtractedIntel) -> None:
    conn.execute(
        """
        INSERT INTO extracted_intel
            (article_id, company, event_type, product_name, capacity_mwh,
             location_state, iso_market, customer, significance_score,
             significance_reason, model_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            MODEL,
        ),
    )


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
    args = parser.parse_args()

    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY environment variable is not set.")

    # Pass key explicitly (stripped). Avoids 'Illegal header value' errors when
    # the env var carries a trailing newline — common when secrets get pasted
    # into GitHub Actions from a file or chat.
    client = anthropic.Anthropic(api_key=api_key)
    conn = init_db()

    articles = fetch_unprocessed(conn, limit=args.limit, prioritize_tags=args.prioritize_tags)
    print(f"BESS Intel Extractor — model: {MODEL}")
    print(f"DB: {DB_PATH}")
    print(f"Tag prioritization: {'ON' if args.prioritize_tags else 'off'}")
    print(f"Unprocessed articles to extract: {len(articles)}\n")

    if not articles:
        print("Nothing to do — all articles already extracted.")
        conn.close()
        return

    results = []
    failed = 0
    total_input = total_output = cache_read = cache_write = 0

    for i, (article_id, title, source, summary) in enumerate(articles, 1):
        try:
            intel, usage = extract_one(client, title, summary or "")
            insert_extraction(conn, article_id, intel)
            results.append((article_id, title, source, intel))

            total_input += usage.input_tokens
            total_output += usage.output_tokens
            cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0

            tag = f"[{intel.significance_score}] {format_field(intel.company)[:24]:<24}"
            print(f"  {i:>3}/{len(articles)}  {tag}  {title[:70]}")
        except anthropic.APIError as e:
            failed += 1
            print(f"  {i:>3}/{len(articles)}  API ERROR: {title[:60]} — {e}")
        except Exception as e:
            failed += 1
            print(f"  {i:>3}/{len(articles)}  ERROR: {title[:60]} — {type(e).__name__}: {e}")

        if i % BATCH_SIZE == 0:
            conn.commit()
            print(f"      ─── committed batch ({i} processed) ───")

    conn.commit()

    bar = "=" * 90
    print(f"\n{bar}")
    print(f"DONE — {len(results)} extracted, {failed} failed")
    print(f"Token usage — input: {total_input}, output: {total_output}, "
          f"cache_read: {cache_read}, cache_write: {cache_write}")
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
