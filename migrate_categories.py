#!/usr/bin/env python3
"""One-shot backfill: remap legacy category strings to the new 6-topic schema.

Pure text update — no LLM calls. Backs up data/intel.db before touching it.
Idempotent: re-running after success is a no-op (old labels no longer exist).
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "intel.db"

# old category  ->  new category
CATEGORY_MAP = {
    "Adjacent Market":     "Applications & Adjacent",
    "Market Structure":    "Market & Commercial",
    "Direct BESS":         "Deployment & Projects",
    "Policy & Regulation": "Policy & Regulation",       # stable, kept explicit
    "Supply Chain":        "Supply Chain & Manufacturing",
    "M&A":                 "Market & Commercial",
}


def main():
    if not DB_PATH.exists():
        sys.exit(f"DB not found: {DB_PATH}")

    # 1. Backup
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB_PATH.with_name(f"intel.db.bak-{stamp}")
    shutil.copy2(DB_PATH, backup)
    print(f"Backup written: {backup}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print("\nBEFORE:")
    for cat, n in cur.execute(
        "SELECT category, COUNT(*) FROM extracted_intel GROUP BY category ORDER BY COUNT(*) DESC"
    ):
        print(f"  {cat!r:34} {n}")

    try:
        # 2. For M&A rows, set event_type FIRST (while category is still 'M&A'),
        #    only when not already set to merger_acquisition.
        cur.execute(
            "UPDATE extracted_intel "
            "SET event_type = 'merger_acquisition' "
            "WHERE category = 'M&A' "
            "  AND (event_type IS NULL OR event_type != 'merger_acquisition')"
        )
        print(f"\nM&A rows tagged event_type=merger_acquisition: {cur.rowcount}")

        # 3. Remap category strings.
        total = 0
        for old, new in CATEGORY_MAP.items():
            if old == new:
                continue
            cur.execute(
                "UPDATE extracted_intel SET category = ? WHERE category = ?",
                (new, old),
            )
            print(f"  {old!r:20} -> {new!r:32} {cur.rowcount} rows")
            total += cur.rowcount

        conn.commit()
        print(f"\nCommitted. {total} category values rewritten.")
    except Exception:
        conn.rollback()
        print("ERROR — rolled back, DB unchanged (backup also intact).")
        raise

    print("\nAFTER:")
    for cat, n in cur.execute(
        "SELECT category, COUNT(*) FROM extracted_intel GROUP BY category ORDER BY COUNT(*) DESC"
    ):
        print(f"  {cat!r:34} {n}")

    conn.close()


if __name__ == "__main__":
    main()
