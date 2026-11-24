import tempfile
import unittest
from pathlib import Path

from outreach.engine import Engine
from outreach.jobs import JobTracker


class JobTrackerTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "queue.sqlite3"
        engine = Engine(self.db, {
            "name": "River", "role": "Designer", "goal": "founder",
            "proof": "I built a searchable project archive.",
            "proof_url": "https://example.org/archive",
        })
        try:
            self.contact = engine.add({"email": "lead@gmail.com", "company": "Studio",
                                       "context": "I saw your digital museum guide."})
        finally:
            engine.close()
        self.tracker = JobTracker(self.db)

    def test_persists_application_and_next_action_alongside_outreach(self):
        added = self.tracker.add({"title": "Product designer", "company": "Studio",
                                  "posting_url": "https://example.org/careers/product-designer",
                                  "contact_id": self.contact["id"]})
        self.assertEqual("saved", added["stage"])
        self.assertEqual("", added["next_date"])
        edited = JobTracker(self.db).update(added["id"], {
            "stage": "interview", "next_action": "Practice the project walkthrough",
            "next_date": "2026-11-30",
        })
        self.assertEqual(added["created_at"], edited["created_at"])
        self.assertEqual(self.contact["id"], edited["contact_id"])
        self.assertEqual(added["posting_url"], edited["posting_url"])
        self.assertEqual(edited["updated_at"], edited["stage_changed_at"])

        new_note = self.tracker.update(added["id"], {"notes": "Thirty minutes with the design manager"})
        self.assertEqual(edited["stage_changed_at"], new_note["stage_changed_at"])
        self.assertEqual("2026-11-30", new_note["next_date"])
        summary = self.tracker.dashboard()
        self.assertEqual(1, summary["stage_counts"]["interview"])
        self.assertEqual(0, summary["stage_counts"]["saved"])
        self.assertEqual([new_note], summary["jobs"])
        self.assertIsNone(self.tracker.update(added["id"], {"contact_id": None})["contact_id"])

    def test_rejects_invalid_fields_without_creating_or_overwriting_rows(self):
        details = {"title": "Product designer", "company": "Studio"}
        cases = (
            {"title": "  "}, {"posting_url": "javascript:alert(1)"}, {"stage": "maybe"},
            {"next_date": "2026-02-30"}, {"next_date": "20261124"}, {"contact_id": True},
            {"contact_id": self.contact["id"] + 42},
        )
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.tracker.add(details | invalid)
        self.assertEqual([], self.tracker.dashboard()["jobs"])
        added = self.tracker.add(details)
        with self.assertRaises(ValueError):
            self.tracker.update(added["id"], {"stage": "applied", "next_date": "tomorrow"})
        self.assertEqual([added], self.tracker.dashboard()["jobs"])
        with self.assertRaises(LookupError):
            self.tracker.update(added["id"] + 1, {"stage": "closed"})
