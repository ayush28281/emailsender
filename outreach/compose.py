"""Compose outreach from the sender's work and attributable recipient context."""

import re
from urllib.parse import urlsplit

from .research import ResearchError, lookup, recipient_domain, shared_domain


SCENARIOS = {"founder", "role", "referral", "freelance", "dm"}
_HIRING_INBOXES = {"careers", "career", "jobs", "recruiting", "recruitment", "hiring",
                   "talentacquisition", "talent-acquisition"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PLACEHOLDER = re.compile(r"\[(?:your |insert |company|name|role|link|project|skill|specific)[^\]]*\]", re.I)
_SKILL_TOKEN = re.compile(r"[^\W_]+(?:[.+#-][^\W_]+)*(?:\+\+|#)?")


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


def _work_examples(profile):
    default = {"title": "Default work sample", **{
        key: _field(profile, key) for key in ("proof", "proof_url", "skills")
    }}
    samples = [default]
    for item in profile.get("work_samples") or []:
        if not isinstance(item, dict):
            continue
        sample = {key: _field(item, key) for key in ("title", "proof", "proof_url", "skills")}
        if (sample["title"] and sample["proof"] and _web_link(sample["proof_url"])
                and not any(_PLACEHOLDER.search(value) for value in sample.values())):
            samples.append(sample)
    return samples


def _work_example(samples, contact, *, use_previous=False):
    if use_previous and isinstance(contact.get("work_sample"), dict):
        previous = contact["work_sample"]
        for sample in samples:
            if all(sample[key] == previous.get(key) for key in ("proof", "proof_url")):
                return sample

    def tokens(text):
        return " " + " ".join(_SKILL_TOKEN.findall(text.casefold())) + " "

    fields = [(tokens(_field(contact, key)), weight) for key, weight in (
        ("role", 4), ("context", 2), ("idea", 1),
    )]

    def score(sample):
        keywords = {tokens(tag) for tag in sample["skills"].split(",")}
        keywords.discard("  ")
        return sum(weight for phrase, weight in fields if any(tag in phrase for tag in keywords))

    # max keeps the default on a tie and picks the earliest saved match otherwise.
    return max(samples, key=score)


def company_details(contact, draft):
    """Select facts from this draft that can be offered to a different recipient."""
    company = draft.get("company") or contact.get("company")
    if not company or _PLACEHOLDER.search(company):
        return None
    details = {"company": company}
    if draft.get("scenario") in {"referral", "role"}:
        return details
    context, source = draft.get("context", ""), draft.get("source", "")
    if not context or _PLACEHOLDER.search(context) or (source and not _web_link(source)):
        return details
    # An empty input observation and this attributed, first-party quote identify
    # the original composer's public research, including already-saved drafts.
    domain = contact["email"].rsplit("@", 1)[1]
    researched = (not contact.get("context") and context.startswith("Your website mentions “")
                  and urlsplit(source).hostname in {domain, "www." + domain})
    if researched or contact.get("reuse_context") is True:
        details["context"] = context
        if source:
            details["source"] = source
    return details


def _sentence(value):
    return value if value.endswith((".", "?", "!", "…", ".”")) else value + "."


def _hello(contact):
    name = _field(contact, "name")
    return f"Hi {name}," if name else "Hello,"


def _held(result, reason, *fields):
    return dict(result, reason=reason, missing_fields=list(fields))


def _finished_draft(result, body, subject, sections):
    dm = result["scenario"] == "dm"
    words = len(body.split())
    if words > (90 if dm else 180):
        label, key, _ = max(sections, key=lambda item: len(item[2].split()))
        target = "90 words or fewer" if dm else "80–150 words"
        return _held(result, f"This draft is {words} words. Shorten {label} and aim for {target}.", key)
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


def prepare(profile: dict, contact: dict, *, research=True) -> dict:
    scenario = _scenario(profile, contact)
    result = {
        "status": "needs_context", "reason": "", "scenario": scenario, "missing_fields": [],
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
    except ResearchError as exc:
        return _held(result, str(exc), "email")
    if scenario not in SCENARIOS:
        return _held(result, "Choose an outreach goal for this contact.", "scenario")
    if not all((name, sender_role, proof, _web_link(proof_url))):
        return _held(result, "Save your name, role, a concrete work example, and its public link in your profile.", "profile")
    if any(_PLACEHOLDER.search(value) for value in (name, sender_role, skills, proof, proof_url)):
        return _held(result, "Replace the template placeholders in your sender profile.", "profile")
    for key in ("context", "company", "role", "job_url", "name"):
        if _PLACEHOLDER.search(_field(contact, key)):
            return _held(result, "Replace the template placeholders with your own details.", key)
    if scenario == "freelance" and _PLACEHOLDER.search(idea):
        return _held(result, "Replace the placeholder in your freelance proposal.",
                     "idea" if _field(contact, "idea") else "profile")
    if result["source"] and not _web_link(result["source"]):
        return _held(result, "Enter a full web address for the context source.", "source")
    hiring = scenario in {"referral", "role"}
    if hiring and result["job_url"] and not _web_link(result["job_url"]):
        return _held(result, "Enter a full web address for the job listing.", "job_url")
    if hiring and result["job_url"] and not result["role"]:
        return _held(result, "Add the role title from the job listing.", "role")
    need_job = hiring and not result["job_url"]
    need_context = not result["context"] and not hiring
    business = not shared_domain(domain)
    lookup_error = ""
    samples = _work_examples(profile)
    if research and business and (need_job or need_context):
        target = (result["role"] or sender_role) if need_job else ""
        try:
            focus = " ".join((sender_role, proof, *(sample["skills"] for sample in samples)))
            details = lookup(domain, target, focus=focus)
        except ResearchError as exc:
            lookup_error = str(exc) + " "
        else:
            result["company"] = result["company"] or details["company"]
            if need_job and details["job_url"]:
                result.update(job_url=details["job_url"], role=target)
            if need_context and details["snippet"]:
                result["context"] = f'Your website mentions “{details["snippet"]}”.'
                result["source"] = details["source"]
    if not result["company"] and business:
        result["company"] = domain
    if not result["company"]:
        return _held(result, "Add the recipient’s company. This address uses a shared email provider.", "company")
    if hiring and not (result["role"] and result["job_url"]):
        missing = [key for key in ("role", "job_url") if not result[key]]
        return _held(result, lookup_error + "Add the missing details from the current job listing.", *missing)
    if need_context and not result["context"]:
        return _held(result, lookup_error + "Add one specific observation about the company or its work.", "context")
    if scenario == "freelance" and not idea:
        return _held(result, "Add the specific service or improvement you would propose to this company.", "idea")
    if hiring and not result["context"]:
        result["context"] = f'I came across the {result["role"]} opening at {result["company"]}.'
        result["source"] = result["job_url"]

    company, context, role = result["company"], _sentence(result["context"]), result["role"]
    sample = _work_example(samples, {**result, "idea": idea if scenario == "freelance" else ""})
    proof, proof_url = sample["proof"], sample["proof_url"]
    result["work_sample"] = {key: sample[key] for key in ("title", "proof", "proof_url")}
    greeting = _hello(contact)
    article = "an" if sender_role[:1].lower() in "aeiou" else "a"
    title = sender_role if re.match(r"^(a|an|the) ", sender_role, re.I) else f"{article} {sender_role}"
    intro = f"I work as {title}."
    # Additional-sample keywords guide matching. The sample's proof makes the actual claims.
    if skills and sample is samples[0]:
        intro += f" My focus is {skills.rstrip('.')}."
    evidence = _sentence(proof) + f" You can see the work here: {proof_url}"
    sections = [("your proof of work", "profile", proof), ("the contact observation", "context", context)]
    if scenario == "dm":
        body = f"{greeting} {context} {_sentence(proof)} Would you be open to connecting?\n\n{name}\n{proof_url}"
        return _finished_draft(result, body, "", sections)

    sections.append(("your skills description", "profile", skills if sample is samples[0] else ""))
    if scenario == "founder":
        subject = f"Building with the team at {company}"
        ask = "Would your team be open to a conversation about where I could contribute?"
    elif scenario == "freelance":
        subject = f"A project idea for {company}"
        context += " " + _sentence(idea)
        sections.append(("the freelance proposal", "idea" if _field(contact, "idea") else "profile", idea))
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
        sample = _work_example(_work_examples(profile), contact, use_previous=True)
        proof, link = sample["proof"], sample["proof_url"]
        if proof and _web_link(link) and not _PLACEHOLDER.search(proof):
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
