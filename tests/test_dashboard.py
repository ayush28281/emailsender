import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from outreach.mailbox import Batch, Incoming
from outreach.server import LocalServer, Workspace
from outreach.settings import read_mailbox


class TestMailbox:
    sender = "sender@example.org"

    def __init__(self):
        self.verified = False
        self.sent = []
        self.inbox = []

    def verify(self):
        self.verified = True

    def poll(self, cursor, earliest, addresses, ids):
        messages, self.inbox = self.inbox, []
        return Batch(str(int(cursor or 0) + 1), messages)

    def send(self, recipient, subject, body, message_id, references, name):
        self.sent.append((recipient, body, message_id, references))


class DashboardTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        env = patch.dict(os.environ, {"EMAILSENDER_ADDRESS": "", "EMAILSENDER_PASSWORD": ""})
        env.start()
        self.addCleanup(env.stop)
        self.space = Workspace(Path(folder.name))
        self.server = LocalServer(0, self.space)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            conn.request("GET" if data is None else "POST", path,
                         None if data is None else json.dumps(data),
                         {"Content-Type": "application/json", **(headers or {})})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_only_local_json_requests_can_change_the_queue(self):
        for headers in ({"Origin": "https://external.example"}, {"Host": "external.example"}):
            self.assertEqual(403, self.request("/api/run", {}, headers)[0])
        self.assertEqual(415, self.request("/api/run", {}, {"Content-Type": "text/plain"})[0])
        self.assertEqual(400, self.request("/api/profile", {"name": "Sender"})[0])
        self.assertEqual(400, self.request("/api/contacts", {"email": "reader@example.com"})[0])

    def test_reuse_preview_leaves_recipient_on_hold_until_user_applies_it(self):
        self.request("/api/profile", {"name": "River", "role": "Designer", "proof": "I built a reading guide.",
                                      "proof_url": "https://example.org/guide"})
        self.request("/api/contacts", {"email": "first@gmail.com", "company": "Bookshop",
                                       "context": "Your shop publishes an annotated reading list.",
                                       "source": "https://example.org/list", "reuse_context": True})
        status, result = self.request("/api/contacts", {"email": "second@gmail.com"})
        self.assertEqual(200, status)
        held = result["contact"]
        self.assertIn("company", held["missing_fields"])
        path = f'/api/contacts/{held["id"]}'
        self.assertIsNone(self.request(path + "/company-suggestion", {})[1]["suggestion"])
        response, result = self.request(path + "/company-suggestion", {"company": "Bookshop"})
        self.assertEqual(200, response)
        suggestion = result["suggestion"]
        self.assertEqual("Bookshop", suggestion["company"])
        self.assertIn("annotated reading list", suggestion["context"])
        entry = next(c for c in self.request("/api/dashboard")[1]["contacts"] if c["id"] == held["id"])
        self.assertEqual("needs_context", entry["status"])
        self.assertFalse(entry["context"])
        self.assertEqual(400, self.request(path + "/update", {"reuse_context": "false"})[0])
        status, result = self.request(path + "/update", suggestion)
        self.assertEqual(200, status)
        self.assertEqual("ready", result["contact"]["status"])

    def test_profile_through_reply_and_outcome(self):
        fake = TestMailbox()
        profile = {"name": "River", "role": "designer", "proof": "I designed a searchable project archive",
                   "proof_url": "https://example.org/archive", "resume_url": "https://example.org/resume",
                   "auto_respond": True}
        self.assertEqual(200, self.request("/api/profile", profile)[0])
        with patch("outreach.server.create_mailbox", return_value=fake):
            status, _ = self.request("/api/mailbox", {"address": fake.sender, "password": "dummy-local-test"})
        self.assertEqual(200, status)
        self.assertTrue(fake.verified)
        self.assertEqual("dummy-local-test", read_mailbox(self.space.mailbox_path)["password"])
        status, result = self.request("/api/contacts", {"email": "reader@gmail.com"})
        self.assertEqual(200, status)
        self.assertEqual("needs_context", result["contact"]["status"])
        endpoint = f'/api/contacts/{result["contact"]["id"]}'
        status, _ = self.request(endpoint + "/update", {"company": "Bookshop",
                                 "context": "I saw the new reading list on your shop's website."})
        self.assertEqual(200, status)
        self.assertEqual(1, self.request("/api/run", {})[1]["sent"])
        self.assertEqual(1, len(fake.sent))
        first_id = fake.sent[0][2]
        fake.inbox.append(Incoming("<reply@example.org>", "reader@gmail.com", "Re: Bookshop",
                                   "Could you send your resume?", refs=(first_id,)))
        self.assertEqual(1, self.request("/api/run", {})[1]["replies"])
        self.assertEqual(2, len(fake.sent))
        self.assertIn(profile["resume_url"], fake.sent[1][1])
        self.assertIn("<reply@example.org>", fake.sent[1][3])
        self.assertEqual(200, self.request(endpoint + "/outcome", {"value": "call"})[0])
        dashboard = self.request("/api/dashboard")[1]
        self.assertNotIn("dummy-local-test", json.dumps(dashboard))
        self.assertEqual(1, dashboard["metrics"]["delivered"])
        self.assertEqual(1.0, dashboard["metrics"]["reply_rate"])
        self.assertEqual(1.0, dashboard["metrics"]["outcome_rate"])
        self.assertEqual("sent", dashboard["contacts"][0]["auto_response_status"])
        self.assertEqual(200, self.request(endpoint + "/outcome", {"value": "none"})[0])
        self.assertEqual(0, self.request("/api/dashboard")[1]["metrics"]["outcomes"])
        self.assertEqual(400, self.request("/api/contacts", {"email": "Reader@gmail.com"})[0])

    def test_track_a_job_and_review_a_linked_first_email(self):
        profile = {"name": "River", "role": "Designer", "proof": "I built a reading guide.",
                   "proof_url": "https://example.org/guide", "review_initial": True}
        self.assertEqual(200, self.request("/api/profile", profile)[0])
        status, result = self.request("/api/contacts", {"email": "reader@gmail.com", "company": "Bookshop",
                                      "role": "Designer", "job_url": "https://example.org/openings/designer"})
        self.assertEqual(200, status)
        contact = result["contact"]
        self.assertEqual("pending_review", contact["status"])
        status, result = self.request("/api/jobs", {"title": "Designer", "company": "Bookshop",
                                      "posting_url": contact["job_url"], "contact_id": contact["id"]})
        self.assertEqual(200, status)
        job_id = result["job"]["id"]
        self.assertEqual(200, self.request(f"/api/jobs/{job_id}/update",
                                           {"stage": "interview", "next_action": "Prepare portfolio walkthrough"})[0])
        _, dashboard = self.request("/api/dashboard")
        self.assertEqual(1, dashboard["stage_counts"]["interview"])
        self.assertEqual(contact["id"], dashboard["jobs"][0]["contact_id"])
        self.assertEqual("pending_review", dashboard["contacts"][0]["status"])
        status, result = self.request(f'/api/contacts/{contact["id"]}/approve',
                                      {"subject": "Bookshop portfolio", "body": "Here's the work I'd like to share.",
                                       "review_token": contact["review_token"]})
        self.assertEqual(200, status)
        self.assertEqual("ready", result["contact"]["status"])

if __name__ == "__main__":
    unittest.main()
