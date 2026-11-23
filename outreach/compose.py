"""Compose outreach from the sender's work and attributable recipient context."""

import re
from urllib.parse import urlsplit

from .research import ResearchError, lookup, recipient_domain, shared_domain


SCENARIOS = {"founder", "role", "referral", "freelance", "dm"}
_HIRING_INBOXES = {"careers", "career", "jobs", "recruiting", "recruitment", "hiring",
                   "talentacquisition", "talent-acquisition"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PLACEHOLDER = re.compile(r"\[(?:your |insert |company|name|role|link|project|skill|specific)[^\]]*\]", re.I)


def _field(record, key):
    return " ".join(_CONTROL.sub(" ", str(record.get(key) or "")).split())


def _web_link(value):
    try:
        url = urlsplit(value)
        return (url.scheme in {"http", "https"} and bool(url.hostname) and "." in url.hostname
                and url.username is None and url.password is None and url.port in (None, 80, 443)
                and not re.search(r'[\s<>"\\]', value))
    except ValueError:
        return False


def _sentence(value):
    return value if value.endswith((".", "?", "!", "…", ".”")) else value + "."


def _hello(contact):
    name = _field(contact, "name")
    return f"Hi {name}," if name else "Hello,"


def _finished_draft(result, body, subject, sections):
    dm = result["scenario"] == "dm"
    words = len(body.split())
    if words > (90 if dm else 180):
        longest = max(sections, key=lambda field: len(sections[field].split()))
        target = "90 words or fewer" if dm else "80–150 words"
        result["reason"] = f"This draft is {words} words. Shorten {longest} and aim for {target}."
        return result
    return dict(result, status="dm_draft" if dm else "ready", subject=subject, body=body)


def _scenario(profile, contact):
    selected = _field(contact, "scenario").lower()
    if selected and selected != "auto":
        return selected
    goal = _field(profile, "goal").lower()
    if _field(contact, "idea"):
        return "freelance"
    if _field(contact, "job_url") or _field(contact, "role"):
        return "referral" if goal == "referral" else "role"
    mailbox = _field(contact, "email").split("@", 1)[0].lower()
    if goal in {"", "auto", "founder", "role"} and mailbox in _HIRING_INBOXES:
        return "role"
    return "founder" if goal in {"", "auto"} else goal


def prepare(profile: dict, contact: dict) -> dict:
    scenario = _scenario(profile, contact)
    result = {
        "status": "needs_context", "reason": "", "scenario": scenario,
        "company": _field(contact, "company"), "context": _field(contact, "context"),
        "source": _field(contact, "source"), "role": _field(contact, "role"),
        "job_url": _field(contact, "job_url"), "subject": "", "body": "",
    }
    name, sender_role, skills, proof, proof_url = (
        _field(profile, key) for key in ("name", "role", "skills", "proof", "proof_url")
    )
    idea = _field(contact, "idea") or _field(profile, "service_idea")
    try:
        domain = recipient_domain(contact.get("email"))
        if scenario not in SCENARIOS:
            raise ResearchError("Choose an outreach goal for this contact.")
        if not all((name, sender_role, proof, _web_link(proof_url))):
            raise ResearchError("Save your name, role, a concrete work example, and its public link in your profile.")
        input_text = [name, sender_role, skills, proof, proof_url, result["context"], result["company"],
                      result["role"], result["job_url"], _field(contact, "name")]
        if scenario == "freelance":
            input_text.append(idea)
        if any(_PLACEHOLDER.search(value) for value in input_text):
            raise ResearchError("Replace the template placeholders with your own details.")
        if result["source"] and not _web_link(result["source"]):
            raise ResearchError("Enter a full web address for the context source.")
        hiring = scenario in {"referral", "role"}
        if hiring and result["job_url"] and not _web_link(result["job_url"]):
            raise ResearchError("Enter a full web address for the job listing.")
        if hiring and result["job_url"] and not result["role"]:
            raise ResearchError("Add the role title from the job listing.")
        need_job = hiring and not result["job_url"]
        need_context = not result["context"] and not hiring
        if (need_job or need_context) and not shared_domain(domain):
            target = (result["role"] or sender_role) if need_job else ""
            details = lookup(domain, target, focus=" ".join((sender_role, skills, proof)))
            result["company"] = result["company"] or details["company"]
            if need_job and details["job_url"]:
                result.update(job_url=details["job_url"], role=target)
            if need_context and details["snippet"]:
                result["context"] = f'Your website mentions “{details["snippet"]}”.'
                result["source"] = details["source"]
        if not result["company"] and not shared_domain(domain):
            result["company"] = domain
        if not result["company"]:
            raise ResearchError("Add the recipient’s company. This address uses a shared email provider.")
        if hiring and not (result["role"] and result["job_url"]):
            raise ResearchError("Add the exact role title and a current job listing link.")
        if need_context and not result["context"]:
            raise ResearchError("Add one specific observation about the company or its work.")
        if scenario == "freelance" and not idea:
            raise ResearchError("Add the specific service or improvement you would propose to this company.")
        if hiring and not result["context"]:
            result["context"] = f'I came across the {result["role"]} opening at {result["company"]}.'
            result["source"] = result["job_url"]
    except ResearchError as exc:
        result["reason"] = str(exc)
        return result

    company, context, role = result["company"], _sentence(result["context"]), result["role"]
    greeting = _hello(contact)
    article = "an" if sender_role[:1].lower() in "aeiou" else "a"
    title = sender_role if re.match(r"^(a|an|the) ", sender_role, re.I) else f"{article} {sender_role}"
    intro = f"I work as {title}."
    if skills:
        intro += f" My focus is {skills.rstrip('.')}."
    evidence = _sentence(proof) + f" You can see the work here: {proof_url}"
    sections = {"your proof of work": proof, "the contact observation": context}
    if scenario == "dm":
        body = f"{greeting} {context} {_sentence(proof)} Would you be open to connecting?\n\n{name}\n{proof_url}"
        return _finished_draft(result, body, "", sections)

    sections["your skills description"] = skills
    if scenario == "founder":
        subject = f"Building with the team at {company}"
        ask = "Would your team be open to a conversation about where I could contribute?"
    elif scenario == "freelance":
        subject = f"A project idea for {company}"
        context += " " + _sentence(idea)
        sections["the freelance proposal"] = idea
        ask = "Would it be useful if I sent a short outline of how that could work for your team?"
    elif scenario == "referral":
        subject = f"A referral question about the {role} role at {company}"
        context += f' The listing I have in mind is {result["job_url"]}'
        ask = "If the work looks relevant, would you feel comfortable referring me for the role?"
    else:
        subject = f"{role} opportunity at {company}"
        context += f' The listing I have in mind is {result["job_url"]}'
        ask = "Are you still considering candidates for this position?"
    body = "\n\n".join((greeting, context, f"{intro} {evidence}", ask, f"Thanks,\n{name}"))
    return _finished_draft(result, body, subject, sections)


def follow_up(profile: dict, contact: dict, stage: int) -> str:
    """Compose the first or final check-in for an existing email thread."""
    scenario = _scenario(profile, contact)
    role = _field(contact, "role")
    company = _field(contact, "company")
    topics = {"founder": "contributing to your team", "freelance": "the project idea I shared",
              "role": f"the {role} opening" if role else "the open position",
              "referral": f"a referral for the {role} role" if role else "a possible referral"}
    topic = topics.get(scenario, "the opportunity I wrote about")
    if stage == 1:
        message = (f"I wanted to follow up on my note about {topic}. "
                   f"Would it be useful to share anything else about my work{f' for the team at {company}' if company else ''}?")
    elif stage == 2:
        message = (f"One last note about {topic}. If the timing works in the future, "
                   "I’d be happy to pick this up again. Thanks for considering it.")
    else:
        raise ValueError("An email sequence has at most two follow-ups.")
    return "\n\n".join((_hello(contact), message, _field(profile, "name")))


def _reply_content(text):
    lines = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if (stripped.startswith(">") or stripped == "--" or
                re.match(r"^(?:on .{0,450}wrote:|from:\s|[- ]*(?:original message|forwarded message)[- :]*$|"
                         r"begin forwarded message:|_{5,}$|sent from my\b)", stripped, re.I)):
            break
        lines.append(stripped)
    while lines and not lines[-1]:
        lines.pop()
    if (len(lines) >= 2 and re.fullmatch(r"(thanks|thank you|regards|best(?: regards)?|cheers),?",
                                        lines[-2], re.I)
            and re.fullmatch(r"[A-Z][\w'-]*(?: [A-Z][\w'-]*){0,3}", lines[-1])):
        lines = lines[:-2]
    content = " ".join(" ".join(lines).split()).replace("’", "'")
    # Mail clients sometimes wrap the quoted-message attribution across lines.
    return re.split(r"\bon .{1,240}? wrote:\s*", content, maxsplit=1, flags=re.I)[0].strip()


_STOP = re.compile(
    r"\b(unsubscribe|opt[ -]?out|remove (my (email|address)|me)(?: (from|off))?|"
    r"take me off|stop (sending|emailing|e-mailing|contacting|writing|messaging)|"
    r"(do not|don't|never) (contact|email|e-mail|message|follow up|reach out|write to|send (me|us))|"
    r"refrain from (emailing|contacting)|"
    r"no (further|more) (contact|emails?|messages?)|leave me alone)\b", re.I,
)
_STOP_COMMAND = re.compile(
    r"(?:^|[,;.!?]\s*)\s*(?:please\s+)?stop(?:\s+please)?(?=\s*[,;.!?]|\s*$)", re.I,
)
_BOUNCE = re.compile(r"\b(undeliverable|delivery (has )?failed|address (not found|rejected)|"
                     r"delivery status notification \(failure\)|permanent delivery failure|"
                     r"mail delivery (failed|failure)|message (was not|could not be) delivered)\b", re.I)
_AWAY = re.compile(r"\b(out of (the )?office|auto(matic)?[- ]?(reply|response)|"
                   r"on (parental|annual|medical|maternity|paternity) leave)\b", re.I)
_CV = r"(?:your |a |the )?(?:current |latest |updated )?(?:r[eé]sum[eé]|cv)"
_REQUEST = r"(?:(?:could|can|would|will) you (?:please )?|(?:please |kindly )?)"
_RESUME = re.compile(_REQUEST + r"(?:send|share|forward|attach)(?: (?:me|us))? " + _CV +
                     r"(?: (?:please|with (?:me|us)))?", re.I)
_EXPERIENCE = re.compile(
    _REQUEST + r"(?:tell (?:me|us) (?:more |a (?:little|bit) )?about|share|describe|outline) "
    r"(?:your |a |one )?(?:(?:relevant |recent |previous |past )?(?:experience|project|work|"
    r"work example|example of your work)|background)|"
    r"what (?:relevant |previous )?experience do you have|what (?:did|have) you buil(?:d|t)", re.I,
)
_DECLINE = re.compile(
    r"(?:sorry[, ]+)?(?:no thanks|no thank you|(?:i(?:'m| am) |we(?:'re| are) )?not interested"
    r"|we(?:'re| are) (?:not hiring|not recruiting)|we (?:do not|don't) have (?:any |an? )?"
    r"(?:openings?|opportunities|jobs?|roles?|vacancies)|(?:there (?:are|is) |we have )?no "
    r"(?:openings?|opportunities|vacancies|roles?)|(?:the|this|that) (?:position|role) "
    r"(?:is|has been) (?:closed|filled)|not (?:right |for )?now)"
    r"(?: (?:at (?:the moment|this time)|right now|currently|for now))?", re.I,
)
_CALL = re.compile(r"\b(?:schedule|book|arrange|join|set up|have|hop on|jump on) (?:a |an )?"
                   r"(?:quick |short |video |phone |introductory |intro )?(?:call|chat|meeting|interview)"
                   r"|\b(?:gmeet|google meet|zoom|teams) (?:call|meeting)"
                   r"|\b(?:are you available|your availability|when (?:are you|would you be) free)\b", re.I)
_POSITIVE = re.compile(r"\b(sounds (good|great|interesting)|(?:i'm|we're|i am|we are) interested|"
                       r"(?:i'd|we'd) love to (?:hear|learn) more|happy to discuss|"
                       r"(?:pleased|delighted|happy) to (?:offer you|extend (?:you )?an offer)|"
                       r"please find (?:attached|enclosed)[\w ,&-]{1,65}offer letter)\b", re.I)


def suggest_reply(profile: dict, contact: dict, text: str) -> tuple[str, str]:
    """Only suggest automatic text for single, fully understood requests."""
    visible = _reply_content(text)
    if _STOP.search(visible) or _STOP_COMMAND.search(visible):
        return "opt_out", ""
    if _BOUNCE.search(visible):
        return "bounce", ""
    if _AWAY.search(visible):
        return "auto_reply", ""
    content = re.sub(r"^(?:hi|hello|hey)(?: [\w'-]+){0,2}[,!]\s*", "", visible, flags=re.I)
    content = re.sub(r"^(?:thanks|thank you)(?: for (?:reaching out|your (?:email|message|note)))?[.,! ]+",
                     "", content, flags=re.I)
    content = re.sub(r"[.,!? ]+(?:thanks|thank you|cheers|best regards|best)[.,! ]*$",
                     "", content, flags=re.I).strip(" \t.,!?")
    if sum(bool(pattern.search(content)) for pattern in (_RESUME, _EXPERIENCE, _DECLINE, _CALL)) > 1:
        return "other", ""
    kind, message = "other", ""
    if _RESUME.fullmatch(content):
        kind = "resume"
        resume_url = _field(profile, "resume_url")
        if _web_link(resume_url) and not re.search(r"\battach\b", content, re.I):
            message = f"Thanks for asking. You can find my résumé here: {resume_url}"
    elif _EXPERIENCE.fullmatch(content):
        kind = "experience"
        proof, link = _field(profile, "proof"), _field(profile, "proof_url")
        if proof and _web_link(link):
            message = f"{_sentence(proof)} Here’s the project: {link}"
    elif _DECLINE.fullmatch(content):
        kind = "decline"
        message = "Thanks for letting me know. I appreciate your time and wish the team well."
    elif _CALL.search(content) and not re.search(
        r"\b(?:cannot|can't|don't want to|unable to)\b.{0,45}\b(?:call|meet|schedule|talk)\b", content, re.I
    ):
        kind = "call"
        availability = _field(profile, "availability")
        if availability:
            message = f"I’d be glad to talk. My available times are {availability.rstrip('.')}. Would one of those work for you?"
    elif _POSITIVE.search(content):
        kind = "positive"
    if message:
        message = "\n\n".join((_hello(contact), message, _field(profile, "name")))
    return kind, message
