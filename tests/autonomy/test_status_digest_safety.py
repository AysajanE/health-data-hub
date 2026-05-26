from __future__ import annotations

import unittest

from scripts.keel_status_digest import digest_status


class StatusDigestSafetyTests(unittest.TestCase):
    def test_passed_plus_pending_is_running_not_passed(self) -> None:
        payload = {
            "run_id": "run_1",
            "items": [
                {"id": "01", "state": "passed"},
                {"id": "02", "state": "pending"},
            ],
        }
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "running")

    def test_manual_gate_dominates_passed(self) -> None:
        payload = {
            "run_id": "run_1",
            "items": [
                {"id": "01", "state": "passed"},
                {"id": "02", "state": "awaiting_human_gate"},
            ],
        }
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "awaiting_human_gate")

    def test_all_items_passed_is_passed(self) -> None:
        payload = {
            "run_id": "run_1",
            "items": [
                {"id": "01", "state": "passed"},
                {"id": "02", "state": "passed"},
            ],
        }
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "passed")

    def test_kernel_terminal_counts_all_passed_is_passed(self) -> None:
        payload = {
            "run_id": "run_1",
            "kernel_status": {
                "current_state": "ST130_PASSED",
                "terminal_counts": {"ST130_PASSED": 6},
            },
        }
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "passed")
        self.assertEqual(digest["kernel_terminal_counts"], {"ST130_PASSED": 6})

    def test_kernel_terminal_counts_partial_running_is_running(self) -> None:
        payload = {
            "run_id": "run_1",
            "kernel_status": {
                "current_state": "ST040_RUNNING",
                "terminal_counts": {"ST130_PASSED": 5, "ST040_RUNNING": 1},
            },
        }
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "running")

    def test_kernel_terminal_counts_blocked_external_dominates(self) -> None:
        payload = {
            "run_id": "run_1",
            "kernel_status": {
                "current_state": "ST130_PASSED",
                "terminal_counts": {"ST130_PASSED": 5, "ST130_BLOCKED_EXTERNAL": 1},
            },
        }
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "blocked_external")


if __name__ == "__main__":
    unittest.main()
