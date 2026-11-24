"""Persistent single-mailbox outreach sequences. All external delivery is explicit in tick()."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import make_msgid
from pathlib import Path

from . import compose
from .job_signals import REPLY_SCHEMA, record_reply
from .mailbox import Mailbox, SendRejected, address
from .research import shared_domain


def now():
    return datetime.now(timezone.utc)


def after_business_days(start, days):
    local = start.astimezone().replace(tzinfo=None)
    while days:
        local += timedelta(days=1)
        if local.weekday() < 5:
            days -= 1
    return local.astimezone(timezone.utc).isoformat()


def _subject(value):
    return re.sub(r"^(?:(?:re|fw|fwd):\s*)+", "", value.strip().lower()).strip()


def _clean_input(data):
    fields = ("name", "company", "scenario", "context", "source", "role", "job_url", "idea")
    result = {k: str(data.get(k) or "").strip() for k in fields}
    result["scenario"] = result["scenario"] or "auto"
    result["email"] = address(data.get("email") or "")
    result["reuse_context"] = data.get("reuse_context") is True and bool(result["context"])
    return result


def _company_key(value):
    return " ".join(value.casefold().split())


def _automatic(kind, profile, suggestion):
    # compose.suggest_reply requires a full, single-intent match and supporting
    # profile data before producing a response for any of these three kinds.
    return bool(profile.get("auto_respond") and suggestion and kind in {"resume", "experience", "decline"})


def _require_review(draft, required):
    if required:
        draft["review_required"] = True
        if draft["status"] == "ready":
            draft.update(status="pending_review", reason="Review the first email before it can send.",
                         review_token=uuid.uuid4().hex)
    return draft


class Engine:
    def __init__(self, db_path, profile, mailbox=None):
        self.profile = dict(profile)
        self.mailbox = mailbox
        self._mutex = threading.RLock()
        path = str(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=20, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS contacts(
                id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE, input TEXT NOT NULL,
                draft TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, sent_at TEXT, next_due TEXT, followups INTEGER NOT NULL DEFAULT 0,
                reply_kind TEXT, reply_text TEXT, replied_at TEXT, positive INTEGER NOT NULL DEFAULT 0,
                suggestion TEXT, reply_id TEXT, auto_response_status TEXT, outcome TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS attempts(
                message_id TEXT PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id),
                stage INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempts_contact ON attempts(contact_id);
            CREATE TABLE IF NOT EXISTS attempt_content(
                message_id TEXT PRIMARY KEY REFERENCES attempts(message_id),
                subject TEXT NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS incoming(key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.executescript(REPLY_SCHEMA)

    def close(self):
        with self._mutex:
            self.db.close()

    @contextmanager
    def _tx(self):
        with self._mutex:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _get(self, contact_id):
        row = self.db.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if row is None:
            raise LookupError("Contact does not exist.")
        return row

    def _view(self, row):
        flat = dict(row)
        raw = json.loads(flat.pop("input"))
        draft = json.loads(flat.pop("draft"))
        item = {**raw, **draft, **flat}
        if item["status"] != "needs_context":
            item["missing_fields"] = []
        elif "missing_fields" not in draft:
            # Recover the next prompt for existing queues without running a lookup
            # or changing a saved draft. A profile edit may already have fixed it.
            item["missing_fields"] = compose.prepare(self.profile, item, research=False)["missing_fields"]
        return item

    def preview(self, contact_id):
        with self._mutex:
            return self._view(self._get(contact_id))

    def messages(self, contact_id):
        """Read recorded delivery attempts and rehearse the current outreach sequence."""
        with self._mutex:
            item = self._view(self._get(contact_id))
            history = []
            for row in self.db.execute("""SELECT a.stage,a.state,a.created_at,c.subject,c.body
                FROM attempts a LEFT JOIN attempt_content c USING(message_id)
                WHERE a.contact_id=? AND a.state IN ('accepted','unknown')
                ORDER BY a.created_at""", (contact_id,)):
                entry = dict(row)
                if row["stage"] == 0 and row["subject"] is None and row["state"] == "accepted":
                    # The first email's saved draft was already immutable after submission.
                    entry.update(subject=item["subject"], body=item["body"])
                history.append(entry)
        sequence = []
        if item.get("subject") and item.get("body"):
            sequence.append({"stage": 0, "subject": item["subject"], "body": item["body"]})
            sequence.extend({"stage": stage, "subject": "Re: " + item["subject"],
                             "body": compose.follow_up(self.profile, item, stage)}
                            for stage in (1, 2))
        return {"history": history, "sequence": sequence}

    def company_suggestion(self, contact_id, company=None):
        """Return copyable company-level details. Reading a suggestion never enrolls a contact."""
        with self._mutex:
            target = self._get(contact_id)
            if target["status"] not in ("needs_context", "ready", "pending_review", "dm_draft"):
                return None
            target_raw = json.loads(target["input"])
            domain = target["email"].rsplit("@", 1)[1]
            explicit = _company_key(company if company is not None else target_raw.get("company", ""))
            shared = shared_domain(domain)
            if not shared and explicit == domain:
                explicit = ""
            if shared and not explicit:
                return None
            matches = []
            for row in self.db.execute("SELECT email,input,draft FROM contacts WHERE id!=? ORDER BY id DESC", (contact_id,)):
                raw, draft = json.loads(row["input"]), json.loads(row["draft"])
                other_domain = row["email"].rsplit("@", 1)[1]
                details = compose.company_details(raw, draft)
                if not details:
                    continue
                same_domain = not shared and other_domain == domain
                same_company = explicit and _company_key(details["company"]) == explicit
                if explicit and not same_company:
                    continue
                if same_domain or (same_company and (shared or shared_domain(other_domain))):
                    matches.append(details)
            # A business domain can service multiple brands. Without an explicit
            # company, ambiguous domains cannot establish which brand to reuse.
            names = {_company_key(d["company"]) for d in matches}
            if len(names) > 1:
                matches = [d for d in matches if _company_key(d["company"]) != domain]
            if not matches or len({_company_key(d["company"]) for d in matches}) != 1:
                return None
            return next((d for d in matches if "context" in d), matches[0])

    def add(self, contact):
        raw = _clean_input(contact)
        with self._mutex:
            if self.db.execute("SELECT 1 FROM contacts WHERE email=?", (raw["email"],)).fetchone():
                raise ValueError("This address already has an outreach record.")
        draft = _require_review(compose.prepare(self.profile, dict(raw)), self.profile.get("review_initial"))
        with self._tx() as db:
            try:
                created = now().isoformat()
                cur = db.execute("""INSERT INTO contacts(email,input,draft,status,reason,created_at,next_due)
                    VALUES(?,?,?,?,?,?,?)""", (raw["email"], json.dumps(raw), json.dumps(draft), draft["status"],
                    draft.get("reason", ""), created, created if draft["status"] == "ready" else None))
            except sqlite3.IntegrityError:
                raise ValueError("This address already has an outreach record.") from None
            return self._view(self._get(cur.lastrowid))

    def update(self, contact_id, contact):
        with self._mutex:
            row = self._get(contact_id)
            if row["status"] not in ("needs_context", "ready", "pending_review", "dm_draft"):
                raise ValueError("Only an unsent contact can be edited.")
            previous = json.loads(row["input"])
            raw = _clean_input({**previous, **contact})
            if "reuse_context" not in contact and any(raw[k] != previous.get(k, "") for k in ("company", "source", "context")):
                raw["reuse_context"] = False
            if raw["email"] != row["email"]:
                raise ValueError("Add a separate contact for a different address.")
        saved_draft = json.loads(row["draft"])
        working = dict(raw)
        if not any(key in contact for key in ("company", "context", "source")) and not raw["context"]:
            # Completing another field (e.g. a freelance proposal) should keep
            # the company's already-researched observation and its source.
            saved = compose.company_details(previous, saved_draft)
            if saved and "context" in saved:
                working.update(saved)
        draft = _require_review(compose.prepare(self.profile, working),
                                saved_draft.get("review_required") or self.profile.get("review_initial"))
        with self._tx() as db:
            current = self._get(contact_id)
            if any(current[key] != row[key] for key in ("status", "input", "draft")):
                raise ValueError("This contact changed. Refresh before editing.")
            db.execute("UPDATE contacts SET input=?,draft=?,status=?,reason=?,next_due=? WHERE id=?",
                       (json.dumps(raw), json.dumps(draft), draft["status"], draft.get("reason", ""),
                        now().isoformat() if draft["status"] == "ready" else None, contact_id))
            return self._view(self._get(contact_id))

    def approve(self, contact_id, subject, body, token):
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 200 or not subject.isprintable():
            raise ValueError("Enter a single-line subject of 200 characters or fewer.")
        if (not isinstance(body, str) or not body.strip() or len(body) > 6000
                or any(not c.isprintable() and c not in "\r\n\t" for c in body)):
            raise ValueError("Enter an email message of 6,000 characters or fewer.")
        with self._tx() as db:
            row = self._get(contact_id)
            draft = json.loads(row["draft"])
            if row["status"] != "pending_review" or not token or token != draft.get("review_token"):
                raise ValueError("This draft changed or already left review. Reopen the recipient details.")
            draft.pop("review_token")
            draft.update(status="ready", reason="", subject=subject.strip(), body=body.strip())
            db.execute("UPDATE contacts SET draft=?,status='ready',reason='',next_due=? WHERE id=?",
                       (json.dumps(draft), now().isoformat(), contact_id))
            return self._view(self._get(contact_id))

    def stop(self, contact_id):
        with self._tx() as db:
            row = self._get(contact_id)
            if row["status"] not in ("suppressed", "bounced", "stopped"):
                db.execute("""UPDATE contacts SET status='stopped',next_due=NULL,reason='Stopped by you.',
                    auto_response_status=CASE WHEN auto_response_status='pending' THEN 'skipped'
                    ELSE auto_response_status END WHERE id=?""", (contact_id,))
            return self._view(self._get(contact_id))

    def outcome(self, contact_id, value):
        with self._tx() as db:
            row = self._get(contact_id)
            value = str(value or "").strip()
            if value == "none":
                value = ""
            if len(value) > 180:
                raise ValueError("Keep the outcome under 180 characters.")
            db.execute("UPDATE contacts SET outcome=? WHERE id=?", (value, contact_id))
            if value and row["status"] in ("ready", "pending_review", "waiting"):
                db.execute("UPDATE contacts SET status='completed',reason='Outcome recorded.',next_due=NULL WHERE id=?",
                           (contact_id,))
            return self._view(self._get(contact_id))

    def dashboard(self):
        with self._mutex:
            rows = [self._view(r) for r in self.db.execute("SELECT * FROM contacts ORDER BY id DESC")]
            followup_times = {}
            for row in self.db.execute("""SELECT contact_id,stage,created_at FROM attempts
                WHERE state='accepted' AND stage IN (1,2) ORDER BY created_at"""):
                followup_times.setdefault(row["contact_id"], []).append(
                    {"number": row["stage"], "sent_at": row["created_at"]})
            for contact in rows:
                contact["followup_history"] = followup_times.get(contact["id"], [])
        delivered = [r for r in rows if r["sent_at"] and r["status"] != "bounced"]
        count = len(delivered)
        replied = sum(bool(r["replied_at"]) for r in delivered)
        positive = sum(bool(r["positive"]) for r in delivered)
        outcomes = sum(bool(r["outcome"]) for r in delivered)
        def rate(n):
            return round(100 * n / count, 1) if count else 0
        return {"contacts": rows, "metrics": {"contacts": len(rows), "delivered": count, "replied": replied,
                "positive": positive, "outcomes": outcomes, "reply_rate": rate(replied),
                "positive_reply_rate": rate(positive), "outcome_rate": rate(outcomes)}}

    def _setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _receive(self, db, msg):
        if db.execute("SELECT 1 FROM incoming WHERE key=?", (msg.key,)).fetchone():
            return False
        row = None
        separate_thread = False
        for ref in reversed(msg.refs):
            row = db.execute("""SELECT c.* FROM attempts a JOIN contacts c ON c.id=a.contact_id
                WHERE a.message_id=? AND a.state!='rejected'""", (ref,)).fetchone()
            if row:
                break
        if not row:
            for email in msg.failed_to if msg.bounced else (msg.sender,):
                r = db.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
                if not r:
                    continue
                first = db.execute("SELECT min(created_at) FROM attempts WHERE contact_id=? AND state!='rejected'",
                                   (r["id"],)).fetchone()[0]
                sent = bool(first)
                if first and msg.received_at:
                    # INTERNALDATE is second-granular. Re-indexing must not attribute
                    # a conversation that predates this person's first campaign.
                    sent = msg.received_at >= datetime.fromisoformat(first).replace(microsecond=0)
                draft = json.loads(r["draft"])
                # Allow opt-outs received in a new thread, including before the first send.
                optout = compose.suggest_reply(self.profile, self._view(r), msg.text)[0] == "opt_out"
                if optout or (sent and (msg.bounced or _subject(msg.subject) == _subject(draft["subject"]))):
                    row = r
                    break
                if sent and not msg.automated:
                    row, separate_thread = r, True
                    break
        if not row:
            return False
        db.execute("INSERT INTO incoming VALUES(?)", (msg.key,))
        if row["status"] in ("suppressed", "bounced"):
            return False
        kind, suggestion = compose.suggest_reply(self.profile, self._view(row), msg.text)
        if msg.bounced:
            kind, suggestion = "bounce", ""
        elif msg.automated and kind != "opt_out":
            return False
        elif separate_thread:
            # Exact correspondent, different conversation. Pause without attributing
            # its content or an automatic response to this outreach thread.
            if row["status"] in ("waiting", "uncertain", "replied"):
                db.execute("""UPDATE contacts SET status='stopped',next_due=NULL,
                    reason='This person sent a separate message. Check your inbox before contacting them.',
                    auto_response_status=CASE WHEN auto_response_status='pending' THEN 'skipped'
                    ELSE auto_response_status END WHERE id=?""", (row["id"],))
            return False
        elif kind in ("auto_reply", "bounce"):
            # Ambiguous reply text alone cannot establish a delivery failure or automatic response.
            kind, suggestion = "other", ""
        status = {"bounce": "bounced", "opt_out": "suppressed"}.get(kind, "replied")
        if row["status"] == "stopped" and status == "replied":
            status = "stopped"
        automatic = row["auto_response_status"]
        if automatic in (None, "pending", "skipped"):
            allowed = (status == "replied" and row["email"] == msg.sender and
                       _automatic(kind, self.profile, suggestion))
            automatic = "pending" if allowed else "skipped"
        accepted_at = row["sent_at"]
        if not accepted_at and kind != "bounce":
            attempted = db.execute("""SELECT created_at FROM attempts WHERE contact_id=? AND stage=0
                AND state!='rejected' ORDER BY created_at DESC LIMIT 1""", (row["id"],)).fetchone()
            if attempted:
                accepted_at = attempted[0]
        db.execute("""UPDATE contacts SET status=?,next_due=?,reply_kind=?,reply_text=?,suggestion=?,
            reply_id=?,positive=max(positive,?),replied_at=CASE WHEN ?='bounce' THEN replied_at
            ELSE coalesce(replied_at,?) END,auto_response_status=?,reason=?,sent_at=? WHERE id=?""",
            (status, now().isoformat() if automatic == "pending" else None, kind, msg.text, suggestion,
             msg.key if re.fullmatch(r"<[^<>\s]+>", msg.key) else None, int(kind in ("positive", "resume", "call", "experience")),
             kind, now().isoformat(), automatic, "Delivery failed." if kind == "bounce" else "Recipient replied.",
             accepted_at, row["id"]))
        if (status in ("replied", "stopped") and msg.sender == row["email"] and accepted_at
                and kind not in {"opt_out", "bounce", "auto_reply"}):
            record_reply(db, row["id"], msg.key, msg.text, now().isoformat())
        return True

    def _poll(self, mailbox, report):
        with self._tx() as db:
            sender = self._setting("sender")
            if sender and sender != mailbox.sender:
                raise ValueError("This database belongs to a different sending address.")
            db.execute("INSERT OR IGNORE INTO settings VALUES('sender',?)", (mailbox.sender,))
            cursor = self._setting("cursor")
            earliest = db.execute("SELECT min(created_at) FROM contacts").fetchone()[0]
            addresses = {r[0] for r in db.execute("SELECT email FROM contacts")}
            ids = {r[0] for r in db.execute("SELECT message_id FROM attempts WHERE state!='rejected'")}
        batch = mailbox.poll(cursor, earliest, addresses, ids)
        with self._tx() as db:
            if self._setting("cursor") != cursor:
                return False
            for msg in batch.messages:
                report["replies"] += int(self._receive(db, msg))
            db.execute("INSERT OR REPLACE INTO settings VALUES('cursor',?)", (batch.cursor,))
        return not batch.backlog

    def _claim(self, mailbox):
        instant = now().isoformat()
        with self._tx() as db:
            row = db.execute("""SELECT * FROM contacts WHERE next_due<=? AND
                (status IN ('ready','waiting') OR (status='replied' AND auto_response_status='pending'))
                ORDER BY next_due,id LIMIT 1""", (instant,)).fetchone()
            if not row:
                return None
            if row["email"] == mailbox.sender:
                db.execute("UPDATE contacts SET status='stopped',reason='Address is the sending mailbox.',next_due=NULL WHERE id=?", (row["id"],))
                return None
            item = self._view(row)
            stage = 0
            body = item["body"]
            if row["status"] == "replied":
                kind, body = compose.suggest_reply(self.profile, item, item["reply_text"])
                if kind != item["reply_kind"] or not _automatic(kind, self.profile, body):
                    db.execute("UPDATE contacts SET auto_response_status='skipped',next_due=NULL WHERE id=?", (row["id"],))
                    return None
                stage = 3
            elif row["status"] == "waiting":
                stage = item["followups"] + 1
                body = compose.follow_up(self.profile, item, stage)
            refs = [r[0] for r in db.execute("""SELECT message_id FROM attempts WHERE contact_id=?
                AND state='accepted' ORDER BY created_at""", (row["id"],))]
            if stage == 3 and item["reply_id"]:
                refs.append(item["reply_id"])
            subject = item["subject"] if not stage else "Re: " + item["subject"]
            mid = make_msgid(idstring=uuid.uuid4().hex, domain=mailbox.sender.rsplit("@", 1)[1])
            db.execute("INSERT INTO attempts VALUES(?,?,?,'unknown',?)", (mid, row["id"], stage, instant))
            db.execute("INSERT INTO attempt_content VALUES(?,?,?)", (mid, subject, body))
            # A process loss after this commit leaves a visible uncertain state, never an automatic resend.
            if stage == 3:
                db.execute("UPDATE contacts SET auto_response_status='uncertain',next_due=NULL WHERE id=?", (row["id"],))
            else:
                db.execute("""UPDATE contacts SET status='uncertain',next_due=NULL,
                    reason='Awaiting delivery confirmation. Check Sent mail if this persists.' WHERE id=?""", (row["id"],))
            return item, mid, stage, subject, body, refs

    def _finish(self, item, mid, stage, rejected=None):
        stamp = now()
        cid = item["id"]
        with self._tx() as db:
            db.execute("UPDATE attempts SET state=? WHERE message_id=?", ("rejected" if rejected else "accepted", mid))
            row = self._get(cid)
            if not rejected and stage == 0:
                db.execute("UPDATE contacts SET sent_at=coalesce(sent_at,?) WHERE id=?", (stamp.isoformat(), cid))
            if not rejected and stage in (1, 2):
                db.execute("UPDATE contacts SET followups=max(followups,?) WHERE id=?", (stage, cid))
            if stage == 3:
                retry = rejected and not rejected.permanent and row["status"] == "replied"
                db.execute("UPDATE contacts SET auto_response_status=?,next_due=? WHERE id=?",
                           ("pending" if retry else "failed" if rejected else "sent",
                            (stamp + timedelta(minutes=15)).isoformat() if retry else None, cid))
                return
            if row["status"] != "uncertain":
                return  # A stop or inbound reply arrived while SMTP was working.
            if rejected:
                status = ("bounced" if rejected.recipient else "stopped") if rejected.permanent else item["status"]
                due = None if rejected.permanent else (stamp + timedelta(minutes=15)).isoformat()
                reason = str(rejected)
            else:
                status = "completed" if stage == 2 else "waiting"
                due = None if stage == 2 else after_business_days(stamp, 5 if stage == 1 else 4)
                reason = "Sequence finished." if stage == 2 else "Waiting for a reply."
            db.execute("UPDATE contacts SET status=?,reason=?,next_due=? WHERE id=?", (status, reason, due, cid))

    def tick(self, stop_requested=None):
        report = {"sent": 0, "replies": 0, "errors": []}
        try:
            with self._mutex:
                active = self.db.execute("""SELECT 1 FROM contacts WHERE status IN ('ready','waiting','uncertain',
                    'completed','replied') OR sent_at IS NOT NULL LIMIT 1""").fetchone()
            if not active:
                return report
            mailbox = self.mailbox or Mailbox.from_env()
            for _ in range(5):
                if stop_requested and stop_requested():
                    break
                if not self._poll(mailbox, report) or (stop_requested and stop_requested()):
                    break
                if self.profile.get("practice_mode"):
                    break
                claim = self._claim(mailbox)
                if not claim:
                    break
                item, mid, stage, subject, body, refs = claim
                try:
                    mailbox.send(item["email"], subject, body, mid, refs, self.profile.get("name", ""))
                except SendRejected as exc:
                    self._finish(item, mid, stage, exc)
                    report["errors"].append(str(exc))
                    break
                self._finish(item, mid, stage)
                report["sent"] += 1
        except Exception as exc:
            # Transport exceptions may contain server-provided text. Avoid displaying credentials or message data.
            if isinstance(exc, ValueError) and str(exc) in ("This database belongs to a different sending address.",
                    "Enter one email address, such as alex@example.com.",
                    "Set EMAILSENDER_PASSWORD to the sending mailbox's app password."):
                report["errors"].append(str(exc))
            else:
                report["errors"].append(f"Mailbox processing paused ({type(exc).__name__}). Check connectivity and credentials.")
        return report
