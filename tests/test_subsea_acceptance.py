from __future__ import annotations

import unittest
from pathlib import Path

from subsea_intervention.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class SubseaAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        job_a = result["job_a"]
        self.assertEqual(job_a["final_state"], "completed")
        self.assertEqual(job_a["frozen_version"], 2)
        self.assertEqual(job_a["outstanding_recovery"], [])
        self.assertEqual(job_a["resources_still_held"], [])
        self.assertEqual(
            [row["gate"] for row in job_a["signoffs"]],
            ["isolation", "start", "pause", "resume", "complete"],
        )
        self.assertTrue({row["signer"] for row in job_a["signoffs"]} >= {"iso", "super", "marine", "commander"})
        self.assertEqual(job_a["late_telemetry"][0]["status"], "incorporated")
        self.assertEqual(result["job_b"]["final_state"], "cancelled")
        self.assertEqual(result["job_b"]["pending_recovery"], [])
        self.assertIn("job-a7", result["job_b"]["contention_message"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
