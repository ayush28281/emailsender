"""Store the exact documents used in a job application."""

import binascii
import sqlite3
from base64 import b64decode
from contextlib import closing


MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_REQUEST = 4 * ((MAX_PDF_BYTES + 2) // 3) + 2048


class Materials:
    def __init__(self, path):
        self.path = path
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS job_materials(
                          job_id INTEGER NOT NULL REFERENCES job_applications(id) ON DELETE CASCADE,
                          kind TEXT NOT NULL, name TEXT NOT NULL, content BLOB NOT NULL,
                          PRIMARY KEY(job_id, kind))""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def list_for_jobs(self):
        items = {}
        with closing(self._connect()) as db:
            for row in db.execute("SELECT job_id,kind,name,length(content) AS size FROM job_materials"):
                items.setdefault(row["job_id"], {})[row["kind"]] = {"name": row["name"], "size": row["size"]}
        return items

    def get(self, job_id, kind):
        with closing(self._connect()) as db:
            row = db.execute("SELECT name,content FROM job_materials WHERE job_id=? AND kind=?",
                             (job_id, kind)).fetchone()
            if not row:
                raise LookupError("No saved PDF for this application.")
            return row["name"], row["content"]

    def put(self, job_id, kind, data):
        remove = data.get("remove") is True
        if not remove:
            name = data.get("name")
            if (not isinstance(name, str) or not name or len(name) > 180 or not name.isprintable()
                    or any(char in name for char in "/\\:")
                    or not name.lower().endswith(".pdf")):
                raise ValueError("Choose a PDF with a valid filename.")
            encoded = data.get("content")
            if not isinstance(encoded, str) or len(encoded) > MAX_PDF_REQUEST:
                raise ValueError("Choose a PDF of 5 MiB or less.")
            try:
                content = b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("Could not read that PDF.") from None
            if len(content) > MAX_PDF_BYTES or not content.startswith(b"%PDF-"):
                raise ValueError("Choose a PDF of 5 MiB or less.")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM job_applications WHERE id=?", (job_id,)).fetchone():
                raise LookupError("Save the application before attaching a PDF.")
            if remove:
                db.execute("DELETE FROM job_materials WHERE job_id=? AND kind=?", (job_id, kind))
                return None
            db.execute("""INSERT INTO job_materials VALUES(?,?,?,?)
                          ON CONFLICT(job_id,kind) DO UPDATE SET name=excluded.name,content=excluded.content""",
                       (job_id, kind, name, content))
        return {"name": name, "size": len(content)}
