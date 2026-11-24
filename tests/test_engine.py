import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from outreach.engine import Engine, after_business_days
from outreach.mailbox import Batch, Incoming, Mailbox, SendRejected, _parse_incoming


class MemoryMailbox:
    sender = "ayush@example.com"

    def __init__(self):
        self.outbox = []
        self.inbox = []
        self.send_error = None
        self.poll_error = None
        self.backlog = False

    def send(self, email, subject, body, mid, references, name):
        if self.send_error:
            raise self.send_error
        self.outbox.append((email, subject, body, mid, references))

    def poll(self, cursor, earliest, addresses, ids):
        if self.poll_error:
            raise self.poll_error
        return Batch(str(len(self.inbox)), self.inbox[int(cursor or 0):], self.backlog)


class SequenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "queue.db"
        self.mail = MemoryMailbox()
        self.profile = {"name": "Ayush", "goal": "founder", "role": "backend engineer", "skills": "Python",
                        "proof": "I built a scheduling API.", "proof_url": "https://proof.example.com/api"}
        self.engine = Engine(self.path, self.profile, self.mail)
        self.addCleanup(self.engine.close)
        self.contact = {"email": "lin@example.net", "company": "Example", "scenario": "founder",
                        "context": "Your team publishes benchmark data for every release."}

    def prepare(self):
        return self.engine.add(self.contact)["id"]

    def force_due(self, cid):
        self.engine.db.execute("UPDATE contacts SET next_due='2000' WHERE id=?", (cid,))

    def reply(self, text, subject=None, refs=True, **kwargs):
        mid = self.mail.outbox[0][3]
        thread = self.mail.outbox[0][1]
        self.mail.inbox.append(Incoming(f"<in-{len(self.mail.inbox)}@example.net>", self.contact["email"],
                                        subject or "Re: " + thread, text, (mid,) if refs else (), **kwargs))

    def test_initial_followups_cap_and_threading(self):
        cid = self.prepare()
        self.assertEqual(self.engine.tick()["sent"], 1)
        self.assertEqual(self.engine.tick()["sent"], 0)
        for n in (1, 2):
            self.force_due(cid)
            self.assertEqual(self.engine.tick()["sent"], 1)
            self.assertEqual(self.engine.preview(cid)["followups"], n)
            self.assertEqual(self.mail.outbox[-1][-1], [m[3] for m in self.mail.outbox[:-1]])
        self.force_due(cid)
        self.assertEqual(self.engine.tick()["sent"], 0)
        self.assertEqual(self.engine.preview(cid)["status"], "completed")
        history = self.engine.dashboard()["contacts"][0]["followup_history"]
        self.assertEqual([1, 2], [item["number"] for item in history])
        self.assertLessEqual(history[0]["sent_at"], history[1]["sent_at"])

    def test_review_holds_until_current_draft_is_approved(self):
        self.engine.profile["review_initial"] = True
        contact = self.engine.add(self.contact)
        cid = contact["id"]
        self.assertEqual("pending_review", contact["status"])
        self.assertIsNone(contact["next_due"])
        self.assertEqual(0, self.engine.tick()["sent"])
        revised = self.engine.update(cid, {"context": "Your team published a new benchmark."})
        with self.assertRaisesRegex(ValueError, "draft changed"):
            self.engine.approve(cid, contact["subject"], contact["body"], contact["review_token"])
        with self.assertRaises(ValueError):
            self.engine.approve(cid, "Header\r\nInjection", revised["body"], revised["review_token"])
        self.assertEqual(0, self.engine.tick()["sent"])

        approved = self.engine.approve(cid, "A benchmark question", "I built a scheduling API.\n\nCould we talk?",
                                       revised["review_token"])
        self.assertEqual("ready", approved["status"])
        self.assertEqual(1, self.engine.tick()["sent"])
        self.assertEqual(("A benchmark question", "I built a scheduling API.\n\nCould we talk?"),
                         self.mail.outbox[0][1:3])
        self.force_due(cid)
        self.assertEqual(1, self.engine.tick()["sent"])
        self.assertEqual("Re: A benchmark question", self.mail.outbox[-1][1])

    def test_review_survives_resolving_a_held_contact_and_a_profile_change(self):
        self.engine.profile["review_initial"] = True
        contact = self.engine.add({"email": "recipient@gmail.com"})
        self.assertEqual("needs_context", contact["status"])
        self.engine.profile["review_initial"] = False
        filled = self.engine.update(contact["id"], {"company": "Studio", "context": "Your team built a filter."})
        self.assertEqual("pending_review", filled["status"])
        self.assertEqual(0, self.engine.tick()["sent"])

    def test_calendar_uses_business_days(self):
        start = datetime(2026, 11, 20, 12).astimezone()
        first = datetime.fromisoformat(after_business_days(start, 4)).astimezone()
        final = datetime.fromisoformat(after_business_days(first, 5)).astimezone()
        self.assertEqual(first.date().isoformat(), "2026-11-26")
        self.assertEqual(final.date().isoformat(), "2026-12-03")

    def test_stop_on_reply_before_due_followup_and_metrics(self):
        cid = self.prepare()
        self.engine.tick()
        self.force_due(cid)
        self.reply("Sounds good!", refs=False)
        self.assertEqual(self.engine.tick(), {"sent": 0, "replies": 1, "errors": []})
        self.engine.outcome(cid, "call")
        dashboard = self.engine.dashboard()
        self.assertEqual(dashboard["contacts"][0]["status"], "replied")
        for key in ("reply_rate", "positive_reply_rate", "outcome_rate"):
            self.assertEqual(dashboard["metrics"][key], 100)
        self.engine.outcome(cid, "none")
        self.assertEqual(self.engine.dashboard()["metrics"]["outcomes"], 0)

    def test_unrelated_mail_then_optout_in_a_new_thread(self):
        cid = self.prepare()
        self.engine.tick()
        self.force_due(cid)
        self.reply("Tuesday works for our recurring meeting.", subject="Weekly standup", refs=False)
        self.assertEqual(self.engine.tick(), {"sent": 0, "replies": 0, "errors": []})
        self.assertEqual(self.engine.preview(cid)["status"], "stopped")
        self.assertEqual(self.engine.dashboard()["metrics"]["reply_rate"], 0)
        self.reply("Please do not contact me again.", subject="Remove me", refs=False)
        self.engine.tick()
        self.assertEqual(self.engine.preview(cid)["status"], "suppressed")
        with self.assertRaises(ValueError):
            self.engine.add(dict(self.contact, email="LIN@example.net"))
        with self.assertRaises(ValueError):
            self.engine.update(cid, {"scenario": "role"})

    def test_human_mention_of_failed_delivery_stops_sequence(self):
        cid = self.prepare()
        self.engine.tick()
        self.force_due(cid)
        self.reply("The delivery failed in my test. Could we talk?")
        self.assertEqual(self.engine.tick(), {"sent": 0, "replies": 1, "errors": []})
        self.assertEqual(self.engine.preview(cid)["status"], "replied")

    def test_machine_notification_does_not_count_as_human_reply(self):
        cid = self.prepare()
        self.engine.tick()
        self.reply("I am out of office.", automated=True)
        self.assertEqual(self.engine.tick()["replies"], 0)
        self.assertEqual(self.engine.preview(cid)["status"], "waiting")

    def test_explicit_optout_in_first_sync_prevents_initial_send(self):
        cid = self.prepare()
        self.mail.inbox.append(Incoming("unsub", self.contact["email"], "List", "Please remove me."))
        self.assertEqual(self.engine.tick()["sent"], 0)
        self.assertEqual(self.engine.preview(cid)["status"], "suppressed")

    def test_old_conversation_on_mailbox_reindex_does_not_stop_new_sequence(self):
        cid = self.prepare()
        self.engine.tick()
        self.reply("Let's meet tomorrow.", subject="Weekly meeting", refs=False,
                   received_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(self.engine.tick()["replies"], 0)
        self.assertEqual(self.engine.preview(cid)["status"], "waiting")

    def test_missing_context_can_be_repaired_and_dm_stays_local(self):
        c = self.engine.add({"email": "someone@gmail.com", "scenario": "founder"})
        self.assertEqual(c["status"], "needs_context")
        updated = self.engine.update(c["id"], {"context": "I tried the new editor.", "company": "Product", "scenario": "dm"})
        self.assertEqual(updated["status"], "dm_draft")
        self.assertEqual(self.engine.tick()["sent"], 0)

    def test_legacy_contact_exposes_missing_fields_without_lookup_or_mutation(self):
        from outreach.research import ResearchError

        with patch("outreach.compose.lookup", side_effect=ResearchError("Unavailable")):
            record = self.engine.add({"email": "team@example.net", "scenario": "referral", "role": "Engineer"})
        before = json.loads(self.engine._get(record["id"])["draft"])
        before.pop("missing_fields")
        self.engine.db.execute("UPDATE contacts SET draft=? WHERE id=?", (json.dumps(before), record["id"]))
        with patch("outreach.compose.lookup", side_effect=AssertionError("Unexpected research")):
            contact = self.engine.dashboard()["contacts"][0]
        self.assertEqual(contact["missing_fields"], ["job_url"])
        self.assertEqual(json.loads(self.engine._get(record["id"])["draft"]), before)

    def test_company_suggestion_is_opt_in_and_preserves_private_notes(self):
        saved = self.engine.add(dict(self.contact, source="https://example.net/releases", role="", name="Lin"))
        target = self.engine.add(dict(self.contact, email="dev@example.net"))
        self.assertEqual(self.engine.company_suggestion(target["id"]), {"company": "Example"})
        self.engine.update(saved["id"], {"reuse_context": True})
        expected = {"company": "Example", "context": self.contact["context"], "source": "https://example.net/releases"}
        self.assertEqual(self.engine.company_suggestion(target["id"]), expected)
        self.engine.update(saved["id"], {"context": "I liked your recent talk, Lin."})
        self.assertEqual(self.engine.company_suggestion(target["id"]), {"company": "Example"})

    def test_partial_repair_keeps_previously_researched_context(self):
        fact = {"company": "Example", "snippet": "Validate schema migrations before merging a pull request",
                "source": "https://example.net/", "job_url": ""}
        with patch("outreach.compose.lookup", return_value=fact):
            contact = self.engine.add({"email": "lin@example.net", "scenario": "freelance"})
        self.assertEqual(contact["missing_fields"], ["idea"])
        with patch("outreach.compose.lookup", side_effect=AssertionError("Unexpected repeat lookup")):
            fixed = self.engine.update(contact["id"], {"idea": "I can prototype validation for your fixtures."})
        self.assertEqual(fixed["status"], "ready")
        self.assertEqual(fixed["context"], contact["context"])
        self.assertEqual(fixed["source"], fact["source"])

    def test_suggestion_requires_company_for_personal_email_and_excludes_job_details(self):
        self.engine.add(dict(self.contact, scenario="referral", role="Engineer", job_url="https://example.net/jobs/1",
                             reuse_context=True))
        held = self.engine.add({"email": "someone@gmail.com"})
        self.assertIsNone(self.engine.company_suggestion(held["id"]))
        self.assertIsNone(self.engine.company_suggestion(held["id"], company="Other"))
        self.assertEqual(self.engine.company_suggestion(held["id"], company=" EXAMPLE "), {"company": "Example"})
        self.assertEqual(self.engine.preview(held["id"])["company"], "")
        self.assertEqual(self.mail.outbox, [])

    def test_exact_domain_research_is_reusable_and_ambiguous_brands_are_held(self):
        fact = {"company": "Example", "snippet": "New source-controlled release notes for our toolkit",
                "source": "https://example.net/", "job_url": ""}
        with patch("outreach.compose.lookup", return_value=fact):
            existing = self.engine.add({"email": "lin@example.net"})
            target = self.engine.add({"email": "new@example.net"})
        self.assertEqual(self.engine.company_suggestion(target["id"]),
                         {"company": "Example", "context": existing["context"], "source": fact["source"]})
        self.assertEqual(self.engine.company_suggestion(target["id"], "example.net")["company"], "Example")
        self.engine.add(dict(self.contact, email="other@example.net", company="Another brand", reuse_context=True))
        self.assertIsNone(self.engine.company_suggestion(target["id"]))
        self.assertIsNone(self.engine.company_suggestion(target["id"], "example.net"))
        self.assertEqual(self.engine.company_suggestion(target["id"], "Another brand")["company"], "Another brand")

    def test_disconnected_or_backlogged_inbox_blocks_all_sends(self):
        self.prepare()
        self.mail.poll_error = OSError()
        self.assertTrue(self.engine.tick()["errors"])
        self.mail.poll_error = None
        self.mail.backlog = True
        self.assertEqual(self.engine.tick()["sent"], 0)
        self.mail.backlog = False
        self.assertEqual(self.engine.tick()["sent"], 1)

    def test_shutdown_during_inbox_check_prevents_new_claim(self):
        cid = self.prepare()
        self.assertEqual(self.engine.tick(MagicMock(side_effect=[False, True]))["sent"], 0)
        self.assertEqual(self.engine.preview(cid)["status"], "ready")

    def test_uncertain_delivery_never_retried_after_restart(self):
        cid = self.prepare()
        self.mail.send_error = ConnectionResetError()
        self.assertTrue(self.engine.tick()["errors"])
        other = Engine(self.path, self.profile, self.mail)
        self.addCleanup(other.close)
        self.mail.send_error = None
        self.assertEqual(other.preview(cid)["status"], "uncertain")
        self.assertEqual(other.tick()["sent"], 0)

    def test_refusal_vs_transient_disconnection_before_send(self):
        cid = self.prepare()
        self.mail.send_error = SendRejected("Login refused")
        self.engine.tick()
        self.assertEqual(self.engine.preview(cid)["status"], "ready")
        self.assertEqual(self.engine.tick()["sent"], 0)
        self.force_due(cid)
        self.mail.send_error = SendRejected("Mailbox rejected", True, True)
        self.engine.tick()
        self.assertEqual(self.engine.preview(cid)["status"], "bounced")
        self.assertEqual(self.engine.dashboard()["metrics"]["delivered"], 0)

    def test_dsn_by_failed_recipient_and_delivered_denominator(self):
        cid = self.prepare()
        self.engine.tick()
        self.mail.inbox.append(Incoming("dsn", "postmaster@other.net", "Delivery failed", "",
                                       failed_to=(self.contact["email"],), bounced=True))
        self.engine.tick()
        self.assertEqual(self.engine.preview(cid)["status"], "bounced")
        self.assertEqual(self.engine.dashboard()["metrics"]["delivered"], 0)

    def test_automatic_resume_reply_once_and_no_followups(self):
        self.engine.profile.update(auto_respond=True, resume_url="https://portfolio.example.com/cv.pdf")
        cid = self.prepare()
        self.engine.tick()
        self.reply("Could you send your resume?")
        self.assertEqual(self.engine.tick()["sent"], 1)
        self.assertEqual(self.engine.preview(cid)["auto_response_status"], "sent")
        self.assertIn(self.engine.profile["resume_url"], self.mail.outbox[-1][2])
        self.assertEqual(self.mail.outbox[-1][-1][-1], self.mail.inbox[-1].key)
        self.reply("Please share your resume.")
        self.assertEqual(self.engine.tick()["sent"], 0)
        self.assertEqual(self.engine.preview(cid)["status"], "replied")

    def test_multi_intent_reply_does_not_auto_respond(self):
        self.engine.profile.update(auto_respond=True, resume_url="https://portfolio.example.com/cv.pdf")
        self.prepare()
        self.engine.tick()
        self.reply("Can you send your resume and schedule a call?")
        self.assertEqual(self.engine.tick()["sent"], 0)

    def test_later_optout_cancels_reply_queued_in_same_poll(self):
        self.engine.profile.update(auto_respond=True, resume_url="https://portfolio.example.com/cv.pdf")
        cid = self.prepare()
        self.engine.tick()
        self.reply("Please send your resume.")
        self.reply("Actually, please unsubscribe.")
        self.assertEqual(self.engine.tick()["sent"], 0)
        self.assertEqual(self.engine.preview(cid)["status"], "suppressed")

    def test_mismatched_account_is_paused(self):
        cid = self.prepare()
        self.engine.tick()
        self.mail.sender = "someone.else@example.org"
        self.force_due(cid)
        self.assertTrue(self.engine.tick()["errors"])
        self.assertEqual(len(self.mail.outbox), 1)

    def test_two_workers_cannot_claim_same_delivery(self):
        self.prepare()
        started = threading.Event()
        release = threading.Event()
        original = self.mail.send
        def slow_send(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError("Test delivery stalled")
            original(*args)
        self.mail.send = slow_send
        other = Engine(self.path, self.profile, self.mail)
        self.addCleanup(other.close)
        worker = threading.Thread(target=self.engine.tick)
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            self.assertEqual(other.tick()["sent"], 0)
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.mail.outbox), 1)


class MailboxTests(unittest.TestCase):
    def test_realistic_imap_header_and_body_fetch(self):
        box = Mailbox("ayush@example.com", "fake")
        client = MagicMock()
        client.__enter__.return_value = client
        client.select.return_value = ("OK", [b"29"])
        client.response.return_value = ("UIDVALIDITY", [b"90"])
        raw = b"From: lin@example.net\nSubject: Re: Opportunities\nMessage-ID: <in@example.net>\n\nHi there!"
        header = raw.partition(b"\n\n")[0] + b"\n\n"
        # Some servers return the UID after the literal header bytes.
        client.uid.side_effect = [("OK", [b"29"]), ("OK", [(b"19 (BODY[HEADER.FIELDS ...] {79}", header),
                                  b' UID 29 INTERNALDATE "26-Nov-2026 11:58:22 +0530")']),
                                  ("OK", [(b"19 (BODY[]<0> {100}", raw), b")"])]
        with patch.object(box, "_imap", return_value=client):
            batch = box.poll("90:15", None, {"lin@example.net"}, set())
        self.assertEqual(batch.cursor, "90:29")
        incoming = batch.messages[0]
        self.assertEqual((incoming.subject, incoming.text), ("Re: Opportunities", "Hi there!"))
        self.assertEqual(incoming.received_at.isoformat(), "2026-11-26T11:58:22+05:30")

    def test_dsn_envelope_and_main_reply_body(self):
        dsn = b"""From: postmaster@relay.example
Subject: Delivery status notification
Content-Type: multipart/report; report-type=delivery-status; boundary=report

--report
Content-Type: text/plain

Delivery failed.
--report
Content-Type: message/delivery-status

Reporting-MTA: dns; relay.example

Final-Recipient: rfc822; lin@example.net
Action: failed
Status: 5.1.1

--report
Content-Type: text/rfc822-headers

Message-ID: <original@sender.example>

--report--
"""
        parsed = _parse_incoming(dsn, "1:3")
        self.assertEqual(parsed.failed_to, ("lin@example.net",))
        self.assertIn("<original@sender.example>", parsed.refs)
        self.assertTrue(parsed.bounced)
        parsed = _parse_incoming(b"From: lin@example.net\nSubject: Re: Hello\nIn-Reply-To: <x@org.com>\n\nYes!\n> Please unsubscribe", "1:4")
        self.assertEqual((parsed.text, parsed.refs), ("Yes!", ("<x@org.com>",)))

    def test_readonly_inbox_bootstrap_and_same_epoch_uid_range(self):
        box = Mailbox("ayush@example.com", "fake")
        client = MagicMock()
        client.__enter__.return_value = client
        client.select.return_value = ("OK", [b"6"])
        attrs = {"UIDVALIDITY": [b"12"], "UIDNEXT": [b"7"]}
        client.response.side_effect = lambda field: (field, attrs[field])
        with patch.object(box, "_imap", return_value=client):
            result = box.poll(None, None, set(), set())
            self.assertEqual(result.cursor, "12:6")
            client.select.assert_called_once_with("INBOX", readonly=True)
            client.uid.return_value = ("OK", [b"6"])
            result = box.poll("12:6", datetime.now(timezone.utc).isoformat(), {"lin@example.net"}, set())
            self.assertEqual(result.messages, [])
            client.uid.assert_called_once_with("SEARCH", None, "UID", "7:*")

    def test_verify_authenticates_both_without_transmitting(self):
        box = Mailbox("ayush@example.com", "fake")
        with patch("outreach.mailbox.imaplib.IMAP4_SSL") as imap, patch("outreach.mailbox.smtplib.SMTP_SSL") as smtp:
            box.verify()
            imap.return_value.login.assert_called_once_with("ayush@example.com", "fake")
            smtp.return_value.login.assert_called_once_with("ayush@example.com", "fake")
            smtp.return_value.send_message.assert_not_called()
