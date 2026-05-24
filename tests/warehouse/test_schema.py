from __future__ import annotations

from pathlib import Path
import unittest

import duckdb


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "src" / "db" / "schema.sql"
EXPECTED_TABLES = {
    "daily_features",
    "mood_current",
    "mood_entries",
    "sleep_merge_diagnostics",
    "sleep_nights",
}


class SchemaSqlTest(unittest.TestCase):
    def test_schema_creates_expected_core_tables(self) -> None:
        self.assertTrue(SCHEMA_PATH.exists(), f"missing schema file: {SCHEMA_PATH}")

        conn = duckdb.connect(database=":memory:")
        try:
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            rows = conn.execute("SHOW TABLES").fetchall()
        finally:
            conn.close()

        created_tables = {name for (name,) in rows}
        self.assertEqual(created_tables, EXPECTED_TABLES)


if __name__ == "__main__":
    unittest.main()
