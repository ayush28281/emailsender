"""Job applications and next actions stored alongside the outreach queue."""

import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from .settings import clean_fields


JOB_STAGES = ("saved", "applied", "interview", "offer", "closed")
FIELDS = ("title", "company", "posting_url", "stage", "next_action", "next_date", "notes", "contact_id")
DEFAULTS = dict.fromkeys(FIELDS, "") | {"stage": "saved", "contact_id": None}


def _validate(data):
    row = clean_fields(data, FIELDS[:-1])
    for label in ("title", "company"):
        value = row[label]
        if not value or len(value) > 180 or any(ord(char) < 32 for char in value):
            raise ValueError(f"Enter a {label} between 1 and 180 characters on a single line.")
    if row["stage"] not in JOB_STAGES:
        raise ValueError("Choose a valid application stage.")
    if row["posting_url"]:
        try:
            url = urlsplit(row["posting_url"])
            port = url.port  # Also validates the parsed port.
            valid = (url.scheme in {"https", "http"} and url.hostname and url.username is None
                     and (port is None or port > 0)
                     and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in row["posting_url"]))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use a complete http or https URL for the job posting.")
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
                                             contact_id,created_at,updated_at,stage_changed_at)
                VALUES(:title,:company,:posting_url,:stage,:next_action,:next_date,:notes,
                       :contact_id,:stamp,:stamp,:stamp)
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
                    updated_at=:stamp,stage_changed_at=:changed WHERE id=:id
            """, job | {"id": job_id, "stamp": stamp, "changed": changed})
            return self._get(db, job_id)

    def dashboard(self):
        counts = dict.fromkeys(JOB_STAGES, 0)
        with closing(self._connect()) as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM job_applications ORDER BY updated_at DESC,id DESC")]
        for job in jobs:
            counts[job["stage"]] += 1
        return {"jobs": jobs, "stage_counts": counts}
