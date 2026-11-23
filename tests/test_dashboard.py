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


if __name__ == "__main__":
    unittest.main()
