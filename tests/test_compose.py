import socket
import time
import unittest
from unittest.mock import patch

from outreach import compose, research


PROFILE = {
    "name": "Jordan Lee", "role": "engineer", "skills": "database migrations and Python",
    "proof": "I built a migration checker that flags incompatible column types before deployment",
    "proof_url": "https://portfolio.example.org/checker", "resume_url": "https://portfolio.example.org/cv",
    "availability": "Tuesday at 14:00 UTC or Thursday at 16:00 UTC", "goal": "founder",
}
CONTACT = {"email": "jules@acme.org", "name": "Jules", "company": "Acme",
           "context": "I read your release notes on schema migration prechecks."}


class CompositionTests(unittest.TestCase):
    def test_founder_with_saved_context_uses_sender_work(self):
        with patch("outreach.compose.lookup") as lookup:
            result = compose.prepare(PROFILE, CONTACT)
        lookup.assert_not_called()
        self.assertEqual(result["status"], "ready")
        for fact in (CONTACT["context"], PROFILE["proof"], PROFILE["proof_url"]):
            self.assertIn(fact, result["body"])
        self.assertEqual(result["body"].count("?"), 1)

    def test_address_only_records_attributed_company_context(self):
        evidence = {"company": "Acme", "snippet": "Test migration plans against a production snapshot",
                    "source": "https://acme.org/", "job_url": ""}
        with patch("outreach.compose.lookup", return_value=evidence):
            result = compose.prepare(PROFILE, {"email": "build-team@acme.org"})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["source"], evidence["source"])
        self.assertIn(evidence["snippet"], result["body"])
        self.assertTrue(result["body"].startswith("Hello,"))
        self.assertNotIn("build-team", result["body"])

    def test_shared_address_holds_without_inventing_employer(self):
        with patch("outreach.compose.lookup") as lookup:
            result = compose.prepare(PROFILE, {"email": "jules@gmail.com"})
        lookup.assert_not_called()
        self.assertEqual(result["status"], "needs_context")
        self.assertEqual(result["body"], "")
        self.assertEqual(compose.prepare(PROFILE, dict(CONTACT, email="jules@gmail.com"))["status"], "ready")

    def test_lookup_failure_is_actionable(self):
        with patch("outreach.compose.lookup", side_effect=research.ResearchError("Unavailable.")):
            result = compose.prepare(PROFILE, {"email": "jules@acme.org"})
        self.assertEqual(result["status"], "needs_context")
        self.assertIn("Unavailable", result["reason"])

    def test_position_and_referral_include_real_listing(self):
        contact = dict(CONTACT, role="Data Engineer", job_url="https://acme.org/jobs/data")
        for goal, expected in (("founder", "role"), ("referral", "referral")):
            with self.subTest(goal=goal):
                result = compose.prepare(dict(PROFILE, goal=goal), contact)
                self.assertEqual(result["scenario"], expected)
                self.assertIn(contact["role"], result["subject"])
                self.assertIn(contact["job_url"], result["body"])

    def test_requires_an_exact_job_when_no_verified_match(self):
        with patch("outreach.compose.lookup", return_value={
                "company": "Acme", "snippet": "", "source": "", "job_url": ""}) as lookup:
            result = compose.prepare(dict(PROFILE, goal="role"), CONTACT)
        self.assertEqual(lookup.call_args.args, ("acme.org", PROFILE["role"]))
        self.assertEqual(result["status"], "needs_context")

    def test_company_lookup_can_complete_a_role_sequence(self):
        with patch("outreach.compose.lookup", return_value={"company": "Acme", "snippet": "",
                "source": "https://acme.org/", "job_url": "https://acme.org/jobs/engineer"}):
            result = compose.prepare(dict(PROFILE, goal="referral"), {"email": "jules@acme.org"})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["role"], PROFILE["role"])
        self.assertEqual(result["source"], result["job_url"])

    def test_specific_freelance_idea_and_separate_dm(self):
        contact = dict(CONTACT, scenario="freelance")
        self.assertEqual(compose.prepare(PROFILE, contact)["status"], "needs_context")
        result = compose.prepare(PROFILE, dict(contact, idea="I could prototype the precheck against anonymized fixtures"))
        self.assertIn("anonymized fixtures", result["body"])
        self.assertEqual(result["status"], "ready")
        dm = compose.prepare(PROFILE, dict(CONTACT, scenario="dm"))
        self.assertEqual(dm["status"], "dm_draft")
        self.assertFalse(dm["subject"])
        self.assertLess(len(dm["body"].split()), 65)

    def test_recruiting_address_and_freelance_profile_need_only_an_address(self):
        details = {"company": "Acme", "snippet": "Check schema changes against anonymized customer records",
                   "source": "https://acme.org/", "job_url": "https://acme.org/jobs/engineer"}
        with patch("outreach.compose.lookup", return_value=details) as lookup:
            role = compose.prepare(PROFILE, {"email": "recruiting@acme.org"})
            self.assertEqual(lookup.call_args.args, ("acme.org", PROFILE["role"]))
        self.assertEqual((role["scenario"], role["status"]), ("role", "ready"))
        with patch("outreach.compose.lookup", return_value=details):
            profile = dict(PROFILE, goal="freelance", service_idea="I can build a migration validation prototype")
            project = compose.prepare(profile, {"email": "careers@acme.org"})
            specific = compose.prepare(profile, dict(CONTACT, idea="I can review your staging migration plan"))
        self.assertEqual((project["scenario"], project["status"]), ("freelance", "ready"))
        self.assertIn(profile["service_idea"], project["body"])
        self.assertIn(details["snippet"], project["body"])
        self.assertIn("staging migration plan", specific["body"])
        self.assertNotIn(profile["service_idea"], specific["body"])

    def test_proof_and_contact_must_be_valid(self):
        self.assertEqual(compose.prepare(dict(PROFILE, proof_url=""), CONTACT)["status"], "needs_context")
        self.assertEqual(compose.prepare(dict(PROFILE, proof="[your project]"), CONTACT)["status"], "needs_context")
        self.assertEqual(compose.prepare(PROFILE, dict(CONTACT, email="j@a.org\nBcc:x@x.com"))["status"], "needs_context")
        result = compose.prepare(dict(PROFILE, goal="freelance", service_idea="I can build [your project]"), CONTACT)
        self.assertEqual(result["status"], "needs_context")
        self.assertIn("placeholder", result["reason"])

    def test_hold_long_email_and_dm_with_specific_edit(self):
        result = compose.prepare(dict(PROFILE, proof="My work matters. " * 65), CONTACT)
        self.assertEqual(result["status"], "needs_context")
        self.assertIn("Shorten your proof of work", result["reason"])
        self.assertFalse(result["body"])
        result = compose.prepare(PROFILE, dict(CONTACT, scenario="dm", context="The release has shipped. " * 24))
        self.assertEqual(result["status"], "needs_context")
        self.assertIn("Shorten the contact observation", result["reason"])

    def test_two_distinct_followups(self):
        contact = dict(CONTACT, scenario="role", role="Backend engineer")
        self.assertIn(contact["role"], compose.follow_up(PROFILE, contact, 1))
        self.assertIn("last note", compose.follow_up(PROFILE, contact, 2))
        with self.assertRaises(ValueError):
            compose.follow_up(PROFILE, contact, 3)


class ReplyTests(unittest.TestCase):
    def test_resume_reply_ignores_quoted_history(self):
        text = ("Hi Jordan,\nCould you share your CV?\nBest,\nJules\n\n"
                "On Monday, Jordan wrote:\n> Please stop contacting me.")
        kind, reply = compose.suggest_reply(PROFILE, CONTACT, text)
        self.assertEqual(kind, "resume")
        self.assertIn(PROFILE["resume_url"], reply)

    def test_decline_with_courtesy(self):
        kind, reply = compose.suggest_reply(PROFILE, CONTACT, "Thanks for reaching out! We're not hiring at the moment.")
        self.assertEqual(kind, "decline")
        self.assertIn("Thanks", reply)

    def test_experience_and_call_use_saved_facts(self):
        kind, reply = compose.suggest_reply(PROFILE, CONTACT, "Can you tell me more about your experience?")
        self.assertEqual(kind, "experience")
        self.assertIn(PROFILE["proof"], reply)
        kind, reply = compose.suggest_reply(PROFILE, CONTACT, "Let's schedule a call.")
        self.assertEqual(kind, "call")
        self.assertIn(PROFILE["availability"], reply)

    def test_meeting_and_offer_paperwork_from_playbook_examples(self):
        kind, _ = compose.suggest_reply(PROFILE, CONTACT, "Our director would like a Google Meet call tomorrow.")
        self.assertEqual(kind, "call")
        self.assertEqual(compose.suggest_reply(PROFILE, CONTACT,
            "Please find attached your onboarding notes and offer letter. Please review and sign."),
            ("positive", ""))

    def test_ambiguous_or_ungrounded_replies_require_review(self):
        for question in ("Please send your resume and salary expectations.",
                         "Please share your CV. Do you need sponsorship?",
                         "Can you tell me about your experience with COBOL?",
                         "Please send your CV and schedule a call.",
                         "Could you share your CV? I have concerns about your project.",
                         "Please share your résumé.\nThanks,\nCan you start tomorrow?"):
            with self.subTest(question=question):
                self.assertEqual(compose.suggest_reply(PROFILE, CONTACT, question), ("other", ""))
        self.assertEqual(compose.suggest_reply(dict(PROFILE, resume_url=""), CONTACT,
                                               "Please share your résumé."), ("resume", ""))
        self.assertEqual(compose.suggest_reply(PROFILE, CONTACT, "Please attach your CV."), ("resume", ""))

    def test_suppression_wins_and_system_mail_has_no_suggestion(self):
        for text, expected in [
            ("Would you send your CV? Actually please don't contact me again.", "opt_out"),
            ("Unsubscribe", "opt_out"), ("Please remove me.", "opt_out"),
            ("Stop", "opt_out"), ("Hello, please stop. Thanks for understanding.", "opt_out"),
            ("Hi Jordan,\nStop!\nRegards,\nJules", "opt_out"),
            ("Delivery failed. Address not found.", "bounce"), ("I'm out of office.", "auto_reply"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(compose.suggest_reply(PROFILE, CONTACT, text), (expected, ""))


class ResearchTests(unittest.TestCase):
    def test_extracts_visible_attributable_description(self):
        page = research._PageReader()
        page.feed('<meta property="og:site_name" content="Acme"><script>fake claims</script>'
                  '<meta name="description" content="We help teams achieve their goals.">'
                  '<p>Read the privacy policy to manage your account preferences.</p>'
                  '<p>Validate schema migrations against a production snapshot.</p>')
        self.assertEqual(research._snippet(page), "Validate schema migrations against a production snapshot")
        self.assertEqual(page.company, "Acme")

    def test_prefers_public_passage_matching_sender_work(self):
        page = research._PageReader()
        page.feed('<meta name="description" content="A product for people who sell handmade clothes.">'
                  '<p>Our inventory API sends webhooks when a warehouse receives stock.</p>')
        self.assertIn("webhooks", research._snippet(page, focus="I built inventory webhooks in Python"))

    def test_role_lookup_returns_first_party_exact_link(self):
        home = research._PageReader()
        home.feed('<a href="/careers">Careers</a>')
        careers = research._PageReader()
        careers.feed('<a href="https://unrelated.com/">Backend engineer</a>'
                     '<a href="/senior">Senior backend engineer</a>'
                     '<a href="/jobs/backend">Backend engineer</a>')
        with patch("outreach.research._fetch", side_effect=[(home, "https://acme.org/"),
                   (careers, "https://acme.org/careers")]):
            self.assertEqual(research.lookup("acme.org", "Backend engineer")["job_url"],
                             "https://acme.org/jobs/backend")

    def test_private_dns_and_redirects_cannot_be_researched(self):
        for address in ("127.0.0.1", "169.254.169.254", "64:ff9b::7f00:1", "ff02::1"):
            choices = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with self.subTest(address=address), patch("socket.getaddrinfo", return_value=choices):
                with self.assertRaises(research.ResearchError):
                    research._addresses("acme.org", time.monotonic() + 1)
        for url in ("https://127.0.0.1/", "https://unrelated.com/", "https://user@acme.org/",
                    "http://acme.org/", "https://acme.org:8080/"):
            with self.subTest(url=url), self.assertRaises(research.ResearchError):
                research._fetch(url, "acme.org", time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
