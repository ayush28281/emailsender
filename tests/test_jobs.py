import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from outreach.engine import Engine
from outreach.job_signals import classify_reply, record_reply
from outreach.jobs import JobTracker
from outreach.mailbox import Batch, Incoming


class TestMailbox:
    sender = "sender@example.org"

    def __init__(self):
        self.sent = []
        self.incoming = []

    def send(self, recipient, subject, body, message_id, references, name):
        self.sent.append(message_id)

    def poll(self, cursor, earliest, addresses, ids):
        return Batch(str(len(self.incoming)), self.incoming[int(cursor or 0):])


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

    def test_saves_job_description_and_resume_version_links(self):
        application = self.tracker.add({
            "title": "Designer", "company": "Studio",
            "description": "Design an accessible library.\n\n" + ("Example. " * 550),
            "resume_url": "https://example.org/resumes/design.pdf",
            "cover_letter_url": "https://example.org/covers/studio.pdf",
        })
        rebuilt = JobTracker(self.db)
        stored = rebuilt.dashboard()["jobs"][0]
        self.assertEqual(application["description"], stored["description"])
        self.assertEqual(application["resume_url"], stored["resume_url"])
        self.assertEqual(application["cover_letter_url"], stored["cover_letter_url"])
        with self.assertRaisesRegex(ValueError, "cover letter"):
            rebuilt.update(stored["id"], {"cover_letter_url": "javascript:alert(1)"})

    def record(self, reply_key, body):
        with closing(self.tracker._connect()) as db, db:
            db.execute("UPDATE contacts SET status='replied' WHERE id=?", (self.contact["id"],))
            record_reply(db, self.contact["id"], reply_key, body, datetime.now(timezone.utc).isoformat())
        self.tracker.sync_replies()

    def test_application_update_waits_for_approval_and_survives_restart(self):
        job = self.tracker.add({"title": "Designer", "company": "Studio", "stage": "applied",
                                "contact_id": self.contact["id"]})
        self.record("message-one", "We'd like to invite you to a technical interview.\n\n> Previous thread")
        state = self.tracker.dashboard()
        self.assertEqual("applied", state["jobs"][0]["stage"])
        suggestion = state["job_suggestions"][0]
        self.assertEqual("interview", suggestion["stage"])
        self.assertNotIn("Previous thread", suggestion["excerpt"])
        restarted = JobTracker(self.db)
        applied = restarted.resolve_suggestion(job["id"], suggestion["id"], "apply")
        self.assertEqual("interview", applied["stage"])
        self.assertEqual("Review the invitation and reply.", applied["next_action"])
        restarted.sync_replies()
        self.assertEqual([], restarted.dashboard()["job_suggestions"])
        with self.assertRaisesRegex(ValueError, "out of date"):
            restarted.resolve_suggestion(job["id"], suggestion["id"], "apply")

    def test_tentative_match_and_stale_suggestion_do_not_change_job(self):
        links = {"company": "Studio", "stage": "applied", "contact_id": self.contact["id"]}
        junior = self.tracker.add(links | {"title": "Junior Designer"})
        senior = self.tracker.add(links | {"title": "Senior Designer"})
        self.record("message-two", "We'd like to invite you to a technical interview.")
        self.assertEqual([], self.tracker.dashboard()["job_suggestions"])
        self.tracker.update(junior["id"], {"contact_id": None})
        self.tracker.sync_replies()
        suggestion = self.tracker.dashboard()["job_suggestions"][0]
        self.assertEqual(senior["id"], suggestion["job_id"])
        self.tracker.update(senior["id"], {"stage": "offer"})
        self.tracker.update(senior["id"], {"stage": "applied"})
        self.tracker.sync_replies()
        self.assertEqual([], self.tracker.dashboard()["job_suggestions"])
        with self.assertRaisesRegex(ValueError, "out of date"):
            self.tracker.resolve_suggestion(senior["id"], suggestion["id"], "apply")

    def test_latest_signal_supersedes_previous_and_discard_persists(self):
        job = self.tracker.add({"title": "Designer", "company": "Studio", "stage": "applied",
                                "contact_id": self.contact["id"]})
        self.record("interview", "Your interview has been confirmed.")
        old = self.tracker.dashboard()["job_suggestions"][0]
        self.record("decision", "We have decided to move forward with other candidates.")
        latest, = self.tracker.dashboard()["job_suggestions"]
        self.assertEqual("closed", latest["stage"])
        with self.assertRaisesRegex(ValueError, "out of date"):
            self.tracker.resolve_suggestion(job["id"], old["id"], "apply")
        self.assertEqual("applied", self.tracker.resolve_suggestion(job["id"], latest["id"], "dismiss")["stage"])
        self.tracker.sync_replies()
        self.assertEqual([], self.tracker.dashboard()["job_suggestions"])

    def test_only_explicit_recruiter_signals_are_classified(self):
        cases = {
            "We want to set up a chat with you.": None,
            "If we decide to schedule an interview, I'll reach out.": None,
            "We are pleased to offer you the position of Junior Designer.": "offer",
            "We are pleased to offer you a phone screen for the Designer role.": None,
            "Unfortunately we will not be moving forward with your application.": "rejection",
            "Please do not contact me again.\n> We offer you the role.": None,
            "What do you think of this role?\nOn Monday Sam wrote:\nPlease find attached your offer letter.": None,
            "Auto-reply: Your interview is confirmed.": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = classify_reply(text)
                self.assertEqual(expected, result[0] if result else None)

    def test_worker_records_only_the_linked_sender_and_recognizes_the_posting(self):
        recipient = "hiring@gmail.com"
        mailbox = TestMailbox()
        engine = Engine(self.db, {"name": "River", "role": "Designer",
                                  "proof": "I built an accessible library.",
                                  "proof_url": "https://example.org/library"}, mailbox)
        self.addCleanup(engine.close)
        engine.stop(self.contact["id"])
        contact = engine.add({
            "email": recipient, "scenario": "role", "company": "Studio", "role": "Designer",
            "job_url": "https://boards.greenhouse.io/acme/jobs/42?gh_src=tag",
        })
        wanted = self.tracker.add({
            "company": "Studio", "title": "Designer", "stage": "applied", "contact_id": contact["id"],
            "posting_url": "https://job-boards.greenhouse.io/acme/jobs/42/",
        })
        self.tracker.add({
            "company": "Studio", "title": "Designer", "stage": "applied", "contact_id": contact["id"],
            "posting_url": "https://job-boards.greenhouse.io/acme/jobs/75",
        })
        self.assertEqual(1, engine.tick()["sent"])
        mid, = mailbox.sent
        invitation = "We'd like to invite you to a technical interview."
        mailbox.incoming.append(Incoming("<forward@example.org>", "someone@example.org", "Re:", invitation,
                                         refs=(mid,)))
        mailbox.incoming.append(Incoming("<auto@example.org>", recipient, "Re:", invitation,
                                         refs=(mid,), automated=True))
        engine.tick()
        self.tracker.sync_replies()
        self.assertEqual([], self.tracker.dashboard()["job_suggestions"])

        mailbox.incoming.append(Incoming("<actual@example.org>", recipient, "Re:", invitation, refs=(mid,)))
        engine.tick()
        self.tracker.sync_replies()
        suggestion, = self.tracker.dashboard()["job_suggestions"]
        self.assertEqual(wanted["id"], suggestion["job_id"])
        self.assertEqual("interview", suggestion["stage"])
