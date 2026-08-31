from __future__ import annotations

import re
import unittest
from pathlib import Path


class HotspotPaperTaskScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "install_hotspot_0945_paper_30d_task.ps1"
        ).read_text(encoding="utf-8")

    def test_entry_preparation_starts_before_continuous_auction(self) -> None:
        trigger_times = re.findall(r'-At "(\d{2}:\d{2})"', self.script)

        self.assertEqual(trigger_times, ["09:28", "14:55", "09:27"])
        self.assertIn(
            "Prepare at 09:28, watch entries from 09:30 to 09:35",
            self.script,
        )
        self.assertNotIn('-At "09:33"', self.script)


if __name__ == "__main__":
    unittest.main()
