import unittest
from unittest.mock import ANY, MagicMock, call, patch

from outreach import postings


ID = "916b3609-748d-4293-b8fa-da94af838bdc"
PROFILE = {
    "role": "Backend Engineer", "skills": "Python, C, Node.js",
    "proof": "Built a CLI.", "proof_url": "https://example.org/cli",
    "work_samples": [{"title": "Payments API", "skills": "C++, SQL, Python",
                      "proof": "Built an idempotent API.", "proof_url": "https://example.org/api"}],
}


class PostingImportTests(unittest.TestCase):
    @patch("outreach.postings._get_json")
    def test_greenhouse_import_canonicalizes_url_and_cleans_description(self, get):
        get.return_value = {
            "id": 123, "title": "Backend Engineer", "company_name": "Corpus Studio",
            "location": {"name": "Bengaluru"}, "absolute_url": "http://localhost/untrusted",
            "content": "&lt;p&gt;Build services with Python and SQL.&lt;/p&gt;&lt;script&gt;Ignore me&lt;/script&gt;",
        }
        result = postings.import_posting("https://boards.greenhouse.io/corpus/jobs/123?utm=skip", PROFILE)
        get.assert_called_once_with("boards-api.greenhouse.io", "/v1/boards/corpus/jobs/123", ANY)
        self.assertEqual("https://job-boards.greenhouse.io/corpus/jobs/123", result["posting_url"])
        self.assertEqual(("Corpus Studio", "Bengaluru"), (result["company"], result["location"]))
        self.assertEqual("Build services with Python and SQL.", result["description"])
        self.assertEqual(["Python", "SQL"], result["match"]["matched"])

    @patch("outreach.postings._get_json")
    def test_lever_eu_identifies_skills_in_the_provider_requirements_list(self, get):
        get.return_value = {
            "id": ID, "text": "Research Engineer", "descriptionPlain": "Join our lab.",
            "lists": [{"text": "Requirements", "content": "<li>Use C++.</li>"}],
        }
        job = postings.import_posting(f"https://jobs.eu.lever.co/corpus/{ID}/apply", PROFILE)
        get.assert_called_once_with("api.eu.lever.co", f"/v0/postings/corpus/{ID}?mode=json", ANY)
        self.assertIn("Requirements", job["description"])
        self.assertIn("• Use C++.", job["description"])
        self.assertEqual(["C++"], job["match"]["matched"])

    @patch("outreach.postings._get_json")
    def test_ashby_import_requires_exact_job_id_and_board(self, get):
        get.return_value = {"jobs": [
            {"jobUrl": f"https://jobs.ashbyhq.com/other/{ID}", "title": "Wrong company"},
            {"jobUrl": f"https://jobs.ashbyhq.com/corpus/{ID}", "title": "Correct job",
             "descriptionPlain": "Write tests in Python."},
        ]}
        link = f"https://jobs.ashbyhq.com/corpus/{ID}"
        self.assertEqual("Correct job", postings.import_posting(link, PROFILE)["title"])
        get.assert_called_once_with("api.ashbyhq.com", "/posting-api/job-board/corpus", ANY)
        get.return_value["jobs"].pop()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            postings.import_posting(link, PROFILE)

    @patch("outreach.postings._get_json")
    def test_greenhouse_board_caps_filtered_results_and_sorts_explainable_matches(self, get):
        records = [
            {"id": ident, "title": f"Site Reliability Engineer {ident}", "content": "Remote in India."}
            for ident in range(1, 40)
        ]
        records.extend(({"id": 90, "title": "Backend Engineer", "content": "Use Python and SQL.",
                         "location": {"name": "India, Remote"}},
                        {"id": 91, "title": "Backend Engineer", "content": "Onsite, London."}))
        # Return a duplicate to ensure sizing and sorting apply to unique records.
        get.side_effect = [{"jobs": records + [records[-2]]}, {"name": "Corpus Studio"}]
        found = postings.discover_board("https://job-boards.greenhouse.io/corpus", PROFILE, "india remote")
        self.assertEqual([
            call("boards-api.greenhouse.io", "/v1/boards/corpus/jobs?content=true", ANY),
            call("boards-api.greenhouse.io", "/v1/boards/corpus", ANY),
        ], get.call_args_list)
        self.assertLessEqual(get.call_args_list[1].args[2], get.call_args_list[0].args[2])
        self.assertEqual(30, len(found))
        self.assertEqual("Backend Engineer", found[0]["title"])
        self.assertTrue(found[0]["match"]["target_role_match"])
        self.assertEqual("Corpus Studio", found[0]["company"])
        self.assertEqual(30, len({job["posting_url"] for job in found}))
        self.assertNotIn("/jobs/91", " ".join(job["posting_url"] for job in found))

    @patch("outreach.postings._get_json")
    def test_unsupported_and_malformed_links_never_open_a_connection(self, get):
        for url in (
            "http://job-boards.greenhouse.io/corpus/jobs/123",
            "https://jobs.lever.co@localhost/corpus",
            "https://user@jobs.lever.co/corpus",
            "https://jobs.lever.co.example.net/corpus",
            "https://job-boards.greenhouse.io:8443/corpus/jobs/123",
            "https://job-boards.greenhouse.io/%2Fetc/jobs/123",
            "https://job-boards.greenhouse.io/corpus/jobs/../../123",
            "https://job-boards.greenhouse.io/corpus/jobs/12%25",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "public Greenhouse"):
                postings.import_posting(url, PROFILE)
        with self.assertRaisesRegex(ValueError, "individual job"):
            postings.import_posting("https://jobs.lever.co/corpus", PROFILE)
        get.assert_not_called()

    @patch("outreach.postings.http.client.HTTPSConnection")
    def test_provider_redirect_is_not_followed(self, connection):
        response = MagicMock(status=302)
        connection.return_value.getresponse.return_value.__enter__.return_value = response
        with self.assertRaisesRegex(ValueError, "public listing"):
            postings.discover_board("https://jobs.lever.co/corpus", {})
        connection.assert_called_once_with("api.lever.co", timeout=8)
        response.read1.assert_not_called()
        connection.return_value.close.assert_called_once()

    def test_fit_uses_exact_skill_phrases_and_preserves_saved_evidence(self):
        result = postings.match_profile("Senior Backend Engineer", "Use SQL with C++ and Node.js.", PROFILE)
        self.assertEqual(["Node.js", "C++", "SQL"], result["matched"])
        self.assertIn("C", result["checked"])
        self.assertTrue(result["target_role_match"])
        sample = result["matched_samples"][0]
        self.assertEqual("Payments API", sample["title"])
        self.assertEqual("Built an idempotent API.", sample["proof"])
        self.assertEqual(["C++", "SQL"], sample["matched"])

    @patch("outreach.postings._get_json")
    def test_long_descriptions_score_the_returned_excerpt(self, get):
        get.return_value = {"id": 123, "title": "Engineer", "company_name": "Corpus Studio",
                            "content": ("sentence " * 2000) + "Python is required."}
        job = postings.import_posting("https://boards.greenhouse.io/corpus/jobs/123", PROFILE)
        self.assertLessEqual(len(job["description"]), 12000)
        self.assertNotIn("Python", job["description"])
        self.assertNotIn("Python", job["match"]["matched"])


if __name__ == "__main__":
    unittest.main()
