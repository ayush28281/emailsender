"""Job applications and next actions stored alongside the outreach queue."""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl, urlsplit

from .job_signals import ALLOWED_STAGES, NEXT_ACTIONS, REPLY_SCHEMA, STAGES
from .settings import clean_fields


JOB_STAGES = ("saved", "applied", "interview", "offer", "closed")
MATERIAL_FIELDS = ("description", "resume_url", "cover_letter_url")
FIELDS = ("title", "company", "posting_url", "stage", "next_action", "next_date", "notes", *MATERIAL_FIELDS, "contact_id")
DEFAULTS = dict.fromkeys(FIELDS, "") | {"stage": "saved", "contact_id": None}


def _link(value):
    if not value:
        return True
    try:
        url = urlsplit(value)
        port = url.port
        return (url.scheme in {"https", "http"} and url.hostname and url.username is None
                and (port is None or port > 0) and len(value) <= 2048
                and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in value))
    except ValueError:
        return False


def _posting_key(value):
    if not value:
        return None
    url = urlsplit(value)
    host = url.hostname
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    query = sorted((key, part) for key, part in parse_qsl(url.query, keep_blank_values=True)
                   if not key.casefold().startswith("utm_") and key.casefold() not in {"gh_src", "lever-source"})
    return host, url.port, url.path.rstrip("/"), query


def _validate(data):
    row = clean_fields(data, set(FIELDS) - {"contact_id", "description", "resume_url"})
    for label in ("title", "company"):
        value = row[label]
        if not value or len(value) > 180 or any(ord(char) < 32 for char in value):
            raise ValueError(f"Enter a {label} between 1 and 180 characters on a single line.")
    if row["stage"] not in JOB_STAGES:
        raise ValueError("Choose a valid application stage.")
    description = data["description"]
    if not isinstance(description, str) or len(description) > 12000 or "\x00" in description:
        raise ValueError("Enter a job description of 12,000 characters or fewer.")
    row["description"] = description.strip()
    for key, label in (("posting_url", "job posting"), ("resume_url", "résumé"), ("cover_letter_url", "cover letter")):
        value = data[key]
        if not isinstance(value, str) or not _link(value.strip()):
            raise ValueError(f"Use a complete http or https URL for the {label}.")
        row[key] = value.strip()
    if row["next_date"]:
        try:
            if date.fromisoformat(row["next_date"]).isoformat() != row["next_date"]:
                raise ValueError()
        except ValueError:
            raise ValueError("Enter a valid next-action date in YYYY-MM-DD format.") from None
    contact_id = data["contact_id"]
    if contact_id is not None and (type(contact_id) is not int or not 0 < contact_id <= 9_007_199_254_740_991):
        raise ValueError("Choose a contact from the outreach queue.")
    return row | {"contact_id": contact_id}


class JobTracker:
    def __init__(self, db_path):
        self.db_path = db_path
        with closing(self._connect()) as db:
            db.executescript(REPLY_SCHEMA)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS job_applications(
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL, company TEXT NOT NULL, posting_url TEXT NOT NULL,
                    stage TEXT NOT NULL, next_action TEXT NOT NULL, next_date TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, stage_changed_at TEXT NOT NULL
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(job_applications)")}
            for key in MATERIAL_FIELDS:
                if key not in columns:
                    db.execute(f"ALTER TABLE job_applications ADD COLUMN {key} TEXT NOT NULL DEFAULT ''")
            db.execute("""
                CREATE TABLE IF NOT EXISTS job_suggestions(
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER NOT NULL REFERENCES job_applications(id) ON DELETE CASCADE,
                    reply_id INTEGER NOT NULL REFERENCES application_replies(id) ON DELETE CASCADE,
                    stage_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    UNIQUE(job_id,reply_id)
                )
            """)

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _get(db, job_id):
        row = db.execute("SELECT * FROM job_applications WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise LookupError("Job application does not exist.")
        return dict(row)

    @staticmethod
    def _check_contact(db, contact_id):
        if contact_id is not None and not db.execute("SELECT 1 FROM contacts WHERE id=?", (contact_id,)).fetchone():
            raise ValueError("The linked outreach contact does not exist.")

    def add(self, data):
        job = _validate(DEFAULTS | data)
        stamp = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._check_contact(db, job["contact_id"])
            cursor = db.execute("""
                INSERT INTO job_applications(title,company,posting_url,stage,next_action,next_date,notes,
                                             description,resume_url,cover_letter_url,contact_id,
                                             created_at,updated_at,stage_changed_at)
                VALUES(:title,:company,:posting_url,:stage,:next_action,:next_date,:notes,
                       :description,:resume_url,:cover_letter_url,:contact_id,:stamp,:stamp,:stamp)
            """, job | {"stamp": stamp})
            return self._get(db, cursor.lastrowid)

    def update(self, job_id, data):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = self._get(db, job_id)
            job = _validate(current | data)
            if all(job[key] == current[key] for key in FIELDS):
                return current
            self._check_contact(db, job["contact_id"])
            stamp = datetime.now(timezone.utc).isoformat()
            changed = current["stage_changed_at"] if current["stage"] == job["stage"] else stamp
            db.execute("""
                UPDATE job_applications SET title=:title,company=:company,posting_url=:posting_url,stage=:stage,
                    next_action=:next_action,next_date=:next_date,notes=:notes,contact_id=:contact_id,
                    description=:description,resume_url=:resume_url,cover_letter_url=:cover_letter_url,
                    updated_at=:stamp,stage_changed_at=:changed WHERE id=:id
            """, job | {"id": job_id, "stamp": stamp, "changed": changed})
            return self._get(db, job_id)

    @staticmethod
    def _match_job(db, contact_id, draft):
        jobs = db.execute("SELECT * FROM job_applications WHERE contact_id=?", (contact_id,)).fetchall()
        if not jobs:
            return None
        # The outgoing draft identifies the posting when one recruiter covers several jobs.
        # Keep query strings because postings can express the job ID there.
        posting = _posting_key(draft.get("job_url"))
        if posting:
            exact = [job for job in jobs if _posting_key(job["posting_url"]) == posting]
            if len(exact) == 1:
                return exact[0]
            if exact:
                return None
        title = " ".join(draft.get("role", "").casefold().split())
        company = " ".join(draft.get("company", "").casefold().split())
        if title and company:
            exact = [job for job in jobs if " ".join(job["title"].casefold().split()) == title
                     and " ".join(job["company"].casefold().split()) == company
                     and not (posting and job["posting_url"])]
            return exact[0] if len(exact) == 1 else None
        return jobs[0] if len(jobs) == 1 else None

    def sync_replies(self):
        """Create a suggestion for the latest matched human milestone on each active job."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            recent = db.execute("""
                SELECT r.*, c.draft FROM application_replies r
                JOIN contacts c ON c.id=r.contact_id
                WHERE c.status NOT IN ('suppressed','bounced')
                  AND r.id=(SELECT max(id) FROM application_replies WHERE contact_id=r.contact_id)
                  AND EXISTS (SELECT 1 FROM job_applications j WHERE j.contact_id=r.contact_id)
            """).fetchall()
            for reply in recent:
                job = self._match_job(db, reply["contact_id"], json.loads(reply["draft"]))
                if job is not None and job["stage"] in ALLOWED_STAGES[reply["kind"]]:
                    db.execute("""INSERT OR IGNORE INTO job_suggestions(job_id,reply_id,stage_version)
                        VALUES(?,?,?)""", (job["id"], reply["id"], job["stage_changed_at"]))

    def _pending(self, db, job_id=None):
        return db.execute("""
            SELECT s.id, s.job_id, r.kind, r.excerpt, r.received_at AS created_at,
                   r.contact_id, c.draft, j.stage, j.stage_changed_at
            FROM job_suggestions s
            JOIN application_replies r ON r.id=s.reply_id
            JOIN contacts c ON c.id=r.contact_id
            JOIN job_applications j ON j.id=s.job_id
            WHERE s.status='pending' AND s.stage_version=j.stage_changed_at
              AND j.contact_id=r.contact_id AND c.status NOT IN ('suppressed','bounced')
              AND r.id=(SELECT max(id) FROM application_replies WHERE contact_id=r.contact_id)
              AND (? IS NULL OR s.job_id=?)
            ORDER BY r.received_at DESC, s.id DESC
        """, (job_id, job_id)).fetchall()

    def _matches(self, db, pending):
        job = self._match_job(db, pending["contact_id"], json.loads(pending["draft"]))
        return job is not None and job["id"] == pending["job_id"] and job["stage"] in ALLOWED_STAGES[pending["kind"]]

    def resolve_suggestion(self, job_id, suggestion_id, action):
        if action not in ("apply", "dismiss"):
            raise ValueError("Choose Apply or Dismiss for this recruiter reply.")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get(db, job_id)
            suggestion = next((item for item in self._pending(db, job_id) if item["id"] == suggestion_id), None)
            if suggestion is None or not self._matches(db, suggestion):
                raise ValueError("This suggestion is out of date. Refresh your applications.")
            if action == "apply":
                kind = suggestion["kind"]
                next_action = job["next_action"]
                if not next_action and not job["next_date"]:
                    next_action = NEXT_ACTIONS.get(kind, "")
                stamp = datetime.now(timezone.utc).isoformat()
                db.execute("""UPDATE job_applications SET stage=?, next_action=?,
                    stage_changed_at=?, updated_at=? WHERE id=?""",
                           (STAGES[kind], next_action, stamp, stamp, job_id))
            db.execute("UPDATE job_suggestions SET status=? WHERE id=?",
                       ("applied" if action == "apply" else "dismissed", suggestion_id))
            return self._get(db, job_id)

    def dashboard(self):
        counts = dict.fromkeys(JOB_STAGES, 0)
        with closing(self._connect()) as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM job_applications ORDER BY updated_at DESC,id DESC")]
            suggestions = []
            for item in self._pending(db):
                if self._matches(db, item):
                    entry = {key: item[key] for key in ("id", "job_id", "kind", "excerpt", "created_at")}
                    suggestions.append(entry | {"stage": STAGES[item["kind"]]})
        for job in jobs:
            counts[job["stage"]] += 1
        return {"jobs": jobs, "stage_counts": counts, "job_suggestions": suggestions}
