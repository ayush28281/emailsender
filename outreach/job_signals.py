"""Recognize explicit outcomes in replies matched by the mailbox worker."""

import re

from .compose import _reply_content, suggest_reply


REPLY_SCHEMA = """
    CREATE TABLE IF NOT EXISTS application_replies(
        id INTEGER PRIMARY KEY,
        contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        reply_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        excerpt TEXT NOT NULL,
        received_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS application_replies_contact ON application_replies(contact_id, id);
"""

STAGES = {"interview": "interview", "offer": "offer", "rejection": "closed"}
ALLOWED_STAGES = {
    "interview": {"saved", "applied"},
    "offer": {"saved", "applied", "interview"},
    "rejection": {"saved", "applied", "interview"},
}
NEXT_ACTIONS = {
    "interview": "Review the invitation and reply.",
    "offer": "Review the offer and response deadline.",
}

_INTERVIEW = re.compile(
    r"\b(?:invit(?:e|ing) you (?:to|for) (?:participate in |schedule |book )?(?:(?:an?|the|your) )?"
    r"(?:\w+ ){0,3}interview|"
    r"(?:please |you can |can we |could we |to )(?:schedule|book|choose a time for|select a time for)"
    r" (?:the|your) (?:\w+ ){0,3}interview|"
    r"(?:please |could we |can we |we'd like to |we would like to )(?:schedule|book) (?:an?|the|your)"
    r" (?:\w+ ){0,3}interview(?: with you)?"
    r"|your (?:\w+ ){0,3}interview (?:is|has been) (?:scheduled|confirmed))\b", re.I,
)
_OFFER = re.compile(
    r"\b(?:(?:pleased|delighted|excited|happy|would like) to offer you (?:the |a |our )?"
    r"(?:\w+ ){0,5}(?:position|role|job)\b|"
    r"extend (?:you )?(?:an? |our )?(?:formal )?(?:job offer|employment offer|offer of employment)\b|"
    r"please find (?:attached|enclosed) (?:your |the |our )?(?:formal )?offer letter\b)", re.I,
)
_REJECTION = re.compile(
    r"\b(?:(?:decided not to|will not|won't|will no longer|are unable to|will be unable to) "
    r"(?:be )?(?:moving|move|proceeding|proceed)(?: forward)? with your (?:application|candidacy)|"
    r"(?:we have decided to|we(?:'re| are)|we will be|we'll be) "
    r"(?:moving|move|proceeding|proceed)(?: forward)? with other candidates|"
    r"your (?:application|candidacy) (?:has been |was |is )(?:unsuccessful|rejected)|"
    r"you (?:were|have) not (?:been )?selected for (?:this|the) (?:position|role))\b", re.I,
)
_UNCERTAIN = re.compile(
    r"\b(?:if (?:we|the team|they) (?:decide|choose|want|approve|can|could|are able|move|proceed)|"
    r"in the event|provided that|pending approval|whether to|may|might|no longer|cannot|can't|won't|"
    r"not to (?:invite|schedule|offer)|"
    r"previously|you asked whether|wonder(?:ing)? (?:if|whether))\b", re.I,
)
_OFFER_FALSE_POSITIVE = re.compile(
    r"\b(?:interview|conversation|phone screen|call|chance|opportunity|option|ability|invitation|training)\b", re.I,
)


def classify_reply(body):
    """Return a single clear application milestone with an attributed excerpt."""
    visible = _reply_content(body)
    if not visible or suggest_reply({}, {}, visible)[0] in {"opt_out", "bounce", "auto_reply"}:
        return None
    matches = {}
    for sentence in re.split(r"(?<=[.!?])\s+", visible):
        if _REJECTION.search(sentence) and not re.search(r"\b(?:if|unless|in the event) (?:we|the team)\b", sentence, re.I):
            matches["rejection"] = sentence
        if _UNCERTAIN.search(sentence):
            continue
        match = _OFFER.search(sentence)
        if match and not _OFFER_FALSE_POSITIVE.search(match[0]):
            matches["offer"] = sentence
        if _INTERVIEW.search(sentence):
            matches["interview"] = sentence
    if len(matches) != 1:
        return None
    kind, excerpt = next(iter(matches.items()))
    return kind, excerpt if len(excerpt) <= 500 else excerpt[:499] + "…"


def record_reply(db, contact_id, reply_key, body, received_at):
    """Store only recognized milestones in the existing inbox transaction."""
    classified = classify_reply(body)
    if classified is not None:
        kind, excerpt = classified
        db.execute("""INSERT OR IGNORE INTO application_replies
            (contact_id,reply_key,kind,excerpt,received_at) VALUES(?,?,?,?,?)""",
                   (contact_id, reply_key, kind, excerpt, received_at))
