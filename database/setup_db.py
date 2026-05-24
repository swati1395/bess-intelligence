import os
import sqlite3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "intel.db")


def _column_exists(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Create the articles table if it doesn't exist, run migrations, and return a connection."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT    NOT NULL,
            url             TEXT    NOT NULL UNIQUE,
            source_name     TEXT    NOT NULL,
            published_date  TEXT,
            published_at    TEXT,
            summary         TEXT,
            tags            TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Migration: add published_at to pre-existing databases that lack it
    if not _column_exists(cur, "articles", "published_at"):
        cur.execute("ALTER TABLE articles ADD COLUMN published_at TEXT")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_created ON articles(created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS extracted_intel (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id          INTEGER NOT NULL UNIQUE,
            company             TEXT,
            event_type          TEXT,
            product_name        TEXT,
            capacity_mwh        REAL,
            location_state      TEXT,
            iso_market          TEXT,
            customer            TEXT,
            significance_score  INTEGER NOT NULL CHECK (significance_score BETWEEN 1 AND 5),
            significance_reason TEXT NOT NULL,
            model_used          TEXT NOT NULL,
            extracted_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
        )
        """
    )
    # Migration: track which extractions have already triggered a Tier 1 email alert
    if not _column_exists(cur, "extracted_intel", "alerted_at"):
        cur.execute("ALTER TABLE extracted_intel ADD COLUMN alerted_at TIMESTAMP")
    # Migration: category classification (one of 6 BESS intel categories)
    if not _column_exists(cur, "extracted_intel", "category"):
        cur.execute("ALTER TABLE extracted_intel ADD COLUMN category TEXT")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_score ON extracted_intel(significance_score)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_iso ON extracted_intel(iso_market)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_event_type ON extracted_intel(event_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_company ON extracted_intel(company)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_alerted_at ON extracted_intel(alerted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_category ON extracted_intel(category)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_projects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            q_id            TEXT,
            iso_market      TEXT NOT NULL,
            q_status        TEXT,
            q_date          TEXT,
            prop_date       TEXT,
            on_date         TEXT,
            wd_date         TEXT,
            ia_date         TEXT,
            ia_phase        TEXT,
            state           TEXT,
            county          TEXT,
            project_name    TEXT,
            utility         TEXT,
            developer       TEXT,
            cluster         TEXT,
            service         TEXT,
            project_type    TEXT,
            type_clean      TEXT,
            type_1          TEXT,
            type_2          TEXT,
            type_3          TEXT,
            mw_1            REAL,
            mw_2            REAL,
            mw_3            REAL,
            storage_mw      REAL,
            total_mw        REAL,
            is_hybrid       INTEGER NOT NULL DEFAULT 0,
            source_file     TEXT NOT NULL,
            imported_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (q_id, iso_market, source_file)
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_iso ON pipeline_projects(iso_market)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_status ON pipeline_projects(q_status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_developer ON pipeline_projects(developer)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_qdate ON pipeline_projects(q_date)")
    conn.commit()
    return conn


if __name__ == "__main__":
    conn = init_db()
    print(f"Database initialized at: {DB_PATH}")
    conn.close()
