#!/usr/bin/env python3
"""Apply db/schema.sql and bootstrap LangGraph checkpointer tables.

Usage:
    python scripts/setup_production_db.py

Requires DATABASE_URL in environment (or .env).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "schema.sql"


def main() -> int:
    if not SCHEMA.is_file():
        print(f"ERROR: schema not found at {SCHEMA}", file=sys.stderr)
        return 1

    from proxy.config import settings

    if not settings.database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1

    import psycopg

    print(f"Applying schema from {SCHEMA} ...")
    sql = SCHEMA.read_text(encoding="utf-8")
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(sql)
        conn.commit()
    print("Schema applied.")

    print("Setting up LangGraph checkpointer tables ...")
    from agent.graph import setup_checkpointer

    setup_checkpointer()
    print("Checkpointer ready.")

    print("Production DB bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
