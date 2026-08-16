from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_event_log import verify_event_log


def write_fixture(root: Path, rows: list[dict[str, object]], reconciled_ids: list[int]) -> None:
    events = root / "ops/autonomy/events.jsonl"
    events.parent.mkdir(parents=True)
    raw_rows = [json.dumps(row, sort_keys=True) for row in rows]
    events.write_text("\n".join(raw_rows) + "\n", encoding="utf-8")
    max_id = max(int(row["event_id"]) for row in rows)
    (root / "ops/autonomy/autonomy_state.json").write_text(
        json.dumps({"last_event_id": max_id}) + "\n",
        encoding="utf-8",
    )
    duplicate_rows: list[dict[str, object]] = []
    for event_id in reconciled_ids:
        occurrences = [
            {
                "line_number": line_number,
                "row_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            }
            for line_number, (raw, row) in enumerate(zip(raw_rows, rows, strict=True), start=1)
            if row["event_id"] == event_id
        ]
        duplicate_rows.append({"event_id": event_id, "occurrences": occurrences})
    receipt = root / "docs/evidence/event-log-legacy-id-reconciliation-20260816.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "autokeel.event_log_reconciliation.v2",
                "reconciled_through_event_id": max_id,
                "duplicate_event_ids": duplicate_rows,
            }
        )
        + "\n",
        encoding="utf-8",
    )


class VerifyEventLogTests(unittest.TestCase):
    def test_exact_legacy_duplicate_receipt_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(
                root,
                [
                    {"event_id": 1, "event": "one"},
                    {"event_id": 2, "event": "first-two"},
                    {"event_id": 2, "event": "second-two"},
                    {"event_id": 3, "event": "three"},
                ],
                [2],
            )

            report = verify_event_log(root)

            self.assertEqual(report["status"], "ok", report)
            self.assertEqual(report["checks"]["reconciled_duplicate_ids"], [2])

    def test_unreceipted_duplicate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(
                root,
                [
                    {"event_id": 1, "event": "one"},
                    {"event_id": 2, "event": "first-two"},
                    {"event_id": 2, "event": "second-two"},
                ],
                [],
            )

            report = verify_event_log(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("unreconciled duplicate event_id: 2" in error for error in report["errors"]))

    def test_future_non_monotonic_id_fails_even_when_legacy_duplicate_is_receipted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(
                root,
                [
                    {"event_id": 1, "event": "one"},
                    {"event_id": 2, "event": "first-two"},
                    {"event_id": 2, "event": "second-two"},
                    {"event_id": 4, "event": "four"},
                    {"event_id": 3, "event": "late-three"},
                ],
                [2],
            )

            report = verify_event_log(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("not strictly increasing" in error for error in report["errors"]))

    def test_relocated_receipted_rows_fail_even_when_raw_hashes_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(
                root,
                [
                    {"event_id": 1, "event": "one"},
                    {"event_id": 2, "event": "first-two"},
                    {"event_id": 2, "event": "second-two"},
                    {"event_id": 3, "event": "three"},
                ],
                [2],
            )
            events = root / "ops/autonomy/events.jsonl"
            events.write_text("\n" + events.read_text(encoding="utf-8"), encoding="utf-8")

            report = verify_event_log(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(
                any("exact physical-line receipt" in error for error in report["errors"]),
                report,
            )

    def test_swapped_duplicate_order_fails_even_when_hash_set_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [
                {"event_id": 1, "event": "one"},
                {"event_id": 2, "event": "first-two"},
                {"event_id": 2, "event": "second-two"},
                {"event_id": 3, "event": "three"},
            ]
            write_fixture(root, rows, [2])
            events = root / "ops/autonomy/events.jsonl"
            raw_rows = events.read_text(encoding="utf-8").splitlines()
            raw_rows[1], raw_rows[2] = raw_rows[2], raw_rows[1]
            events.write_text("\n".join(raw_rows) + "\n", encoding="utf-8")

            report = verify_event_log(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(
                any("exact physical-line receipt" in error for error in report["errors"]),
                report,
            )

    def test_json_boolean_event_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(root, [{"event_id": True, "event": "not-an-integer-id"}], [])

            report = verify_event_log(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("invalid event_id: True" in error for error in report["errors"]), report)

    def test_json_boolean_receipt_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_fixture(
                root,
                [
                    {"event_id": 1, "event": "one"},
                    {"event_id": 2, "event": "first-two"},
                    {"event_id": 2, "event": "second-two"},
                ],
                [2],
            )
            receipt = root / "docs/evidence/event-log-legacy-id-reconciliation-20260816.json"
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            payload["reconciled_through_event_id"] = True
            payload["duplicate_event_ids"][0]["event_id"] = True
            receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            report = verify_event_log(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(
                any("positive reconciled_through_event_id" in error for error in report["errors"]),
                report,
            )
            self.assertTrue(
                any("invalid event_id" in error for error in report["errors"]),
                report,
            )


if __name__ == "__main__":
    unittest.main()
