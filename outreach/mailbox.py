"""Authenticated mail delivery and incremental, read-only inbox access."""

from __future__ import annotations

import email
from email.headerregistry import Address
from email.message import EmailMessage
from email import policy
from email.utils import formatdate, getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
import imaplib
import os
import re
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta


def address(value: str) -> str:
    value = value.strip()
    if not value.isascii() or len(value) > 254 or any(c.isspace() for c in value):
        raise ValueError("Enter one email address, such as alex@example.com.")
    try:
        parsed = Address(addr_spec=value)
        if (parsed.addr_spec != value or not parsed.username or len(parsed.username) > 64
                or not re.fullmatch(r"(?!-)[A-Za-z0-9-]+(?<!-)(?:\.(?!-)[A-Za-z0-9-]+(?<!-))+", parsed.domain)):
            raise ValueError()
    except (ValueError, IndexError, TypeError, email.errors.HeaderParseError):
        raise ValueError("Enter one email address, such as alex@example.com.") from None
    return value.lower()


class SendRejected(Exception):
    """SMTP explicitly refused delivery, or the connection failed before sending."""

    def __init__(self, message: str, permanent: bool = False, recipient: bool = False):
        super().__init__(message)
        self.permanent = permanent
        self.recipient = recipient


@dataclass
class Incoming:
    key: str
    sender: str
    subject: str
    text: str
    refs: tuple[str, ...] = ()
    failed_to: tuple[str, ...] = ()
    automated: bool = False
    bounced: bool = False
    received_at: datetime | None = None


@dataclass
class Batch:
    cursor: str
    messages: list[Incoming]
    backlog: bool = False


class _PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _references(msg):
    fields = ("in-reply-to", "references", "original-message-id")
    return re.findall(r"<[^<>\s]+>", " ".join(str(msg.get(k, "")) for k in fields))


def _parse_incoming(raw, key):
    msg = email.message_from_bytes(raw, policy=policy.default)
    refs = _references(msg)
    failed_to = []
    delayed = False
    for part in msg.walk():
        if part.get_content_type() == "message/delivery-status":
            for field in part.get_payload():
                refs.extend(_references(field))
                action = str(field.get("Action", "")).lower()
                delayed |= action == "delayed"
                if action == "failed" or str(field.get("Status", "")).startswith("5"):
                    recipient = str(field.get("Final-Recipient", "")).split(";")[-1].strip().lower()
                    if recipient:
                        failed_to.append(recipient)
        if part.get_content_type() == "message/rfc822" and isinstance(part.get_payload(), list):
            for original in part.get_payload():
                refs.extend(re.findall(r"<[^<>\s]+>", str(original.get("message-id", ""))))
        if part.get_content_type() == "text/rfc822-headers":
            original = email.message_from_bytes(part.get_payload(decode=True) or b"", policy=policy.default)
            refs.extend(re.findall(r"<[^<>\s]+>", str(original.get("message-id", ""))))
    sender = next((addr.lower() for _, addr in getaddresses(msg.get_all("from", []))), "")
    body = msg.get_body(preferencelist=("plain", "html"))
    content = ""
    if body is not None:
        try:
            content = body.get_content()
        except (LookupError, UnicodeError, ValueError):
            content = (body.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if body.get_content_type() == "text/html":
            parser = _PlainHTML()
            parser.feed(content)
            content = " ".join(parser.parts)
    # Quoted campaigns can contain request/opt-out terms absent from the actual reply.
    content = re.split(r"(?im)^\s*(?:>.*|On .{2,500}wrote:|From:\s+\S.*|[- ]*Original Message[- ]*)$", content, 1)[0]
    subject = str(msg.get("subject", ""))
    auto = (str(msg.get("Auto-Submitted", "no")).lower() != "no" or delayed
            or bool(re.match(r"(?i)^(?:out of (?:the )?office|automatic reply|auto.?reply)\b", subject)))
    daemon = sender.partition("@")[0] in ("mailer-daemon", "postmaster")
    bounced = bool(failed_to) or (daemon and not delayed and bool(re.search(
        r"undeliver|failure|failed|returned mail", subject, re.I)))
    return Incoming(str(msg.get("message-id", "")).strip() or key, sender, subject,
                    content.strip()[:5000], tuple(refs), tuple(failed_to), auto, bounced)


class Mailbox:
    def __init__(self, sender, password, smtp_host="smtp.gmail.com", smtp_port=465,
                 imap_host="imap.gmail.com", imap_port=993):
        self.sender = address(sender)
        self.password = password
        if not self.password:
            raise ValueError("Set EMAILSENDER_PASSWORD to the sending mailbox's app password.")
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.imap_host = imap_host
        self.imap_port = int(imap_port)

    @classmethod
    def from_env(cls):
        return cls(os.getenv("EMAILSENDER_ADDRESS", ""), os.getenv("EMAILSENDER_PASSWORD"),
                   os.getenv("EMAILSENDER_SMTP_HOST", "smtp.gmail.com"), os.getenv("EMAILSENDER_SMTP_PORT", "465"),
                   os.getenv("EMAILSENDER_IMAP_HOST", "imap.gmail.com"), os.getenv("EMAILSENDER_IMAP_PORT", "993"))

    def _smtp(self):
        ctx = ssl.create_default_context()
        if self.smtp_port == 465:
            client = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=20, context=ctx)
        else:
            client = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20)
        try:
            if self.smtp_port != 465:
                client.starttls(context=ctx)
            client.login(self.sender, self.password)
            return client
        except BaseException:
            client.close()
            raise

    def _imap(self):
        client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=ssl.create_default_context(), timeout=20)
        try:
            client.login(self.sender, self.password)
            return client
        except BaseException:
            with suppress(Exception):
                client.shutdown()
            raise

    def verify(self):
        with self._imap():
            pass
        self._smtp().close()

    def send(self, recipient, subject, body, message_id, references, name):
        msg = EmailMessage()
        msg["From"] = Address(display_name=name, addr_spec=self.sender)
        msg["To"] = address(recipient)
        msg["Subject"] = subject
        msg["Message-ID"] = message_id
        msg["Date"] = formatdate(localtime=True)
        if references:
            msg["In-Reply-To"] = references[-1]
            msg["References"] = " ".join(references)
        msg.set_content(body)
        client = None
        try:
            try:
                client = self._smtp()
            except (smtplib.SMTPException, OSError) as exc:
                raise SendRejected(f"SMTP connection or login failed ({type(exc).__name__}).") from exc
            try:
                client.send_message(msg, from_addr=self.sender, to_addrs=[recipient])
            except smtplib.SMTPRecipientsRefused as exc:
                code = next(iter(exc.recipients.values()))[0]
                raise SendRejected(f"Recipient refused by SMTP ({code}).", code >= 500, True) from exc
            except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
                raise SendRejected(f"Message refused by SMTP ({exc.smtp_code}).", exc.smtp_code >= 500) from exc
            # Disconnects while DATA is in flight have an unknown outcome.
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()

    def poll(self, cursor, earliest, addresses, ids):
        """Scan from enrollment or resume the mailbox's stable UID cursor."""
        with self._imap() as client:
            ok, _ = client.select("INBOX", readonly=True)
            if ok != "OK":
                raise RuntimeError("IMAP could not open INBOX.")
            validity = client.response("UIDVALIDITY")[1][0]
            if validity is None:
                raise RuntimeError("IMAP did not return UIDVALIDITY.")
            epoch = str(int(validity))
            saved_epoch, _, pointer = (cursor or "").partition(":")
            if saved_epoch == epoch:
                start = int(pointer)
                criterion = ("UID", f"{start + 1}:*")
            elif earliest:
                start = 0
                # Server INTERNALDATE can have a different local date near midnight.
                dt = datetime.fromisoformat(earliest) - timedelta(days=1)
                # IMAP date literals use English month names, regardless of OS locale.
                month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[dt.month - 1]
                criterion = ("SINCE", f"{dt.day:02d}-{month}-{dt.year}")
            else:
                next_uid = client.response("UIDNEXT")[1][0]
                if next_uid is not None:
                    return Batch(f"{epoch}:{int(next_uid)-1}", [])
                start = 0
                criterion = ("ALL",)
            ok, data = client.uid("SEARCH", None, *criterion)
            if ok != "OK":
                raise RuntimeError("IMAP inbox search failed.")
            uids = sorted(int(uid) for uid in data[0].split() if int(uid) > start)
            if not ids and not addresses:
                return Batch(f"{epoch}:{max(uids, default=start)}", [])
            batch = uids[:100]
            messages = []
            if batch:
                headers = "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT IN-REPLY-TO REFERENCES CONTENT-TYPE)])"
                ok, entries = client.uid("FETCH", ",".join(str(uid) for uid in batch), headers)
                if ok != "OK":
                    raise RuntimeError("IMAP header fetch failed.")
                for index, item in enumerate(entries):
                    if not isinstance(item, tuple):
                        continue
                    metadata = item[0]
                    if index + 1 < len(entries) and isinstance(entries[index + 1], bytes):
                        metadata += b" " + entries[index + 1]
                    ident = re.search(rb"\bUID (\d+)", metadata, re.I)
                    if not ident:
                        raise RuntimeError("IMAP response omitted the message UID.")
                    uid = ident[1].decode("ascii")
                    head = email.message_from_bytes(item[1], policy=policy.default)
                    senders = {a.lower() for _, a in getaddresses(head.get_all("from", []))}
                    daemon = any(a.partition("@")[0] in ("mailer-daemon", "postmaster") for a in senders)
                    if not (ids.intersection(_references(head)) or addresses.intersection(senders)
                            or daemon or head.get_content_type() == "multipart/report"):
                        continue
                    # Enough for normal replies/delivery status, even with a large attachment following.
                    ok, payload = client.uid("FETCH", uid, "(BODY.PEEK[]<0.262144>)")
                    if ok != "OK":
                        raise RuntimeError("IMAP message fetch failed.")
                    raw = next((x[1] for x in payload if isinstance(x, tuple)), None)
                    if raw:
                        incoming = _parse_incoming(raw, f"{epoch}:{uid}")
                        stamp = re.search(rb'\bINTERNALDATE "([^"]+)"', metadata, re.I)
                        if stamp:
                            incoming.received_at = parsedate_to_datetime(stamp[1].decode("ascii"))
                        messages.append(incoming)
            return Batch(f"{epoch}:{batch[-1] if batch else start}", messages, len(batch) < len(uids))
