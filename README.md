# BESS Market Intelligence System

Competitive intelligence tool for the Battery Energy Storage System (BESS) market, focused on **ERCOT** (Texas) and **CAISO** (California).

## Project Structure

```
.
├── scraper/
│   └── rss_scraper.py        # Module 1: pulls + tags + stores RSS articles
├── database/
│   └── setup_db.py           # SQLite schema bootstrap
├── config/
│   └── sources.yaml          # RSS feed list
├── data/
│   └── intel.db              # SQLite database (created on first run)
├── requirements.txt
└── README.md
```

## Setup

```bash
pip install -r requirements.txt
```

## Module 1 — RSS News Scraper

Pulls from 7 industry feeds, auto-tags articles, dedupes on URL, and stores them in `data/intel.db`.

```bash
python scraper/rss_scraper.py
```

### Auto-tagging categories

| Tag         | Keywords                                                              |
|-------------|-----------------------------------------------------------------------|
| `ERCOT`     | Texas, ERCOT, ECRS, RRS, CPS Energy, AEP Texas                        |
| `CAISO`     | California, CAISO, CPUC, PG&E, SCE, SDG&E                             |
| `TESLA`     | Tesla, Megapack, Powerwall, Autobidder                                |
| `COMPETITOR`| BYD, CATL, Fluence, Samsung SDI, Wärtsilä, Sungrow, Hithium           |
| `POLICY`    | IRA, FEOC, NFPA, interconnection, tariff                              |
| `SAFETY`    | fire, thermal runaway, incident, explosion                            |

Matching is case-insensitive substring over `title + summary`.

### Schema (`articles`)

| Column           | Type      | Notes                                       |
|------------------|-----------|---------------------------------------------|
| `id`             | INTEGER   | PK, autoincrement                           |
| `title`          | TEXT      |                                             |
| `url`            | TEXT      | **UNIQUE** — used for deduplication         |
| `source_name`    | TEXT      |                                             |
| `published_date` | TEXT      | Raw RSS pub date string (as provided)       |
| `published_at`   | TEXT      | ISO 8601 UTC, parsed — used for sorting     |
| `summary`        | TEXT      | HTML stripped                               |
| `tags`           | TEXT      | JSON array, e.g. `["ERCOT","TESLA"]`        |
| `created_at`     | TIMESTAMP | Defaults to `CURRENT_TIMESTAMP`             |

Re-runs are safe — duplicate URLs are skipped silently.
