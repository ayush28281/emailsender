"""Read and compare public job postings from Greenhouse, Lever, and Ashby."""

import http.client
import json
import queue
import re
import threading
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit


_BOARDS = {
    "boards.greenhouse.io": ("Greenhouse", "boards-api.greenhouse.io", "job-boards.greenhouse.io"),
    "job-boards.greenhouse.io": ("Greenhouse", "boards-api.greenhouse.io", "job-boards.greenhouse.io"),
    "jobs.lever.co": ("Lever", "api.lever.co", "jobs.lever.co"),
    "jobs.eu.lever.co": ("Lever", "api.eu.lever.co", "jobs.eu.lever.co"),
    "jobs.ashbyhq.com": ("Ashby", "api.ashbyhq.com", "jobs.ashbyhq.com"),
}
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_UUID = re.compile(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")
_TERMS = re.compile(r"[^\W_]+(?:\.[^\W_]+)*(?:\+\+|#)?")
_MAX_RESPONSE = 32 * 1024 * 1024
_DESCRIPTION_LIMIT = 12_000
_RESULT_LIMIT = 30
_LOOKUP_TIMEOUT = 14
_REQUESTS = threading.BoundedSemaphore(4)
_BAD_LINK = "Paste a public Greenhouse, Lever, or Ashby job or company board link."
_UNAVAILABLE = "This posting or company board is unavailable. Check the link and try again."


class _DescriptionHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template"}:
            self.hidden += 1
        elif not self.hidden and tag == "li":
            self.parts.append("\n• ")
        elif not self.hidden and tag in {"br", "div", "p", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in {"div", "p", "li", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value):
    return value if isinstance(value, str) else ""


def _clean(value, limit):
    return " ".join(_text(value).split())[:limit]


def _html_text(value):
    parser = _DescriptionHTML()
    try:
        parser.feed(unescape(_text(value)[:100_000]))
        parser.close()
    except AssertionError:
        # HTMLParser can reject malformed declarations. Preserve text seen so far.
        pass
    return "".join(parser.parts)


def _tokens(value):
    return " " + " ".join(_TERMS.findall(_text(value).casefold())) + " "


def _keywords(value):
    return list(dict.fromkeys(word.strip() for word in re.split(r"[,;\n]", _text(value)) if word.strip()))


def match_profile(title, description, profile):
    """Compare explicit skill phrases. This is a keyword check, not an eligibility score."""
    posting = _tokens(_text(title) + " " + _text(description))
    role = _tokens(profile.get("role"))
    default = {
        "title": "Default work sample",
        "proof": profile.get("proof"),
        "proof_url": profile.get("proof_url"),
        "skills": profile.get("skills"),
    }
    checked = {}
    samples = []
    for sample in [default, *(profile.get("work_samples") or [])]:
        if not isinstance(sample, dict):
            continue
        keywords = {}
        for shown in _keywords(sample.get("skills")):
            key = _tokens(shown)
            if key != "  ":
                keywords.setdefault(key, shown)
                checked.setdefault(key, shown)
        matches = [shown for key, shown in keywords.items() if key in posting]
        if matches and sample.get("proof") and sample.get("proof_url"):
            samples.append({key: sample[key] for key in ("title", "proof", "proof_url")} | {"matched": matches})
    return {
        "checked": list(checked.values()),
        "matched": [shown for key, shown in checked.items() if key in posting],
        "target_role_match": role != "  " and role in _tokens(title),
        "matched_samples": sorted(samples, key=lambda item: -len(item["matched"])),
    }


def _parse(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError(_BAD_LINK)
    try:
        link = urlsplit(url.strip())
        if (link.scheme != "https" or link.hostname not in _BOARDS or link.username is not None
                or link.password is not None or link.port not in (None, 443)):
            raise ValueError(_BAD_LINK)
    except ValueError:
        raise ValueError(_BAD_LINK) from None
    parts = [unquote(value) for value in link.path.strip("/").split("/")]
    provider, api, site = _BOARDS[link.hostname]
    if not _SLUG.fullmatch(parts[0]):
        raise ValueError(_BAD_LINK)
    board, job = parts[0], ""
    if provider == "Greenhouse" and len(parts) > 1:
        if len(parts) != 3 or parts[1] != "jobs" or not re.fullmatch(r"[0-9]{1,18}", parts[2]):
            raise ValueError(_BAD_LINK)
        job = parts[2]
    elif len(parts) > 1:
        if len(parts) == 3 and parts[-1] in {"apply", "application"}:
            parts.pop()
        if len(parts) != 2 or not _UUID.fullmatch(parts[1]):
            raise ValueError(_BAD_LINK)
        job = parts[1].lower()
    return provider, api, site, board, job


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError()
    return seconds


def _get_json(host, path, deadline):
    # Host and path are composed exclusively from fixed provider names and
    # validated path segments. Use HTTPS directly and never follow redirects.
    connection = http.client.HTTPSConnection(host, timeout=8)
    try:
        connection.timeout = min(8, _remaining(deadline))
        connection.connect()
        sock = connection.sock
        sock.settimeout(_remaining(deadline))
        connection.request("GET", path, headers={"Accept": "application/json", "User-Agent": "Emailsender/1.0"})
        sock.settimeout(_remaining(deadline))
        with connection.getresponse() as response:
            if response.status in (404, 410):
                raise ValueError(_UNAVAILABLE)
            if response.status != 200:
                raise ValueError("The job provider did not return a public listing. Try again later.")
            body = bytearray()
            while True:
                sock.settimeout(_remaining(deadline))
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE:
                    raise ValueError("This company board is too large to import. Enter the job details manually.")
            return json.loads(body)
    except TimeoutError:
        raise ValueError("The job provider took too long to respond. Try again shortly.") from None
    except (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeError):
        raise ValueError("Could not read the job provider. Check your internet connection and try again.") from None
    finally:
        connection.close()


def _result(provider, company, title, url, description, location, profile):
    title = _clean(title, 160)
    if not title:
        return None
    description = "\n".join(" ".join(line.split()) for line in _text(description).splitlines())
    description = re.sub(r"\n{3,}", "\n\n", description).strip()
    if len(description) > _DESCRIPTION_LIMIT:
        excerpt = description[:_DESCRIPTION_LIMIT]
        description = excerpt.rstrip() if description[_DESCRIPTION_LIMIT].isspace() else re.sub(r"\S+$", "", excerpt).rstrip()
    match = match_profile(title, description, profile)
    return {"provider": provider, "company": _clean(company, 160), "title": title, "posting_url": url,
            "description": description, "location": _clean(location, 200), "match": match}


def _greenhouse(data, site, board, company, profile):
    job_id = str(data.get("id", ""))
    if not re.fullmatch(r"[0-9]{1,18}", job_id):
        return None
    location = data.get("location")
    return _result("Greenhouse", _text(data.get("company_name")) or company, data.get("title"),
                   f"https://{site}/{board}/jobs/{job_id}", _html_text(data.get("content")),
                   location.get("name") if isinstance(location, dict) else "", profile)


def _lever(data, site, board, company, profile):
    job_id = _text(data.get("id")).lower()
    if not _UUID.fullmatch(job_id):
        return None
    description = [_text(data.get("descriptionPlain")) or _html_text(data.get("description"))]
    for section in data.get("lists") or []:
        if isinstance(section, dict):
            description.extend((_text(section.get("text")), _html_text(section.get("content"))))
    description.append(_text(data.get("additionalPlain")))
    categories = data.get("categories")
    return _result("Lever", company, data.get("text"), f"https://{site}/{board}/{job_id}",
                   "\n".join(description), categories.get("location") if isinstance(categories, dict) else "", profile)


def _ashby(data, site, board, company, profile):
    # The public API exposes a URL in place of an ID. Validate it before using
    # its identifier, and reconstruct the link on the provider's own host.
    try:
        provider, _, _, posted_board, job_id = _parse(data.get("jobUrl"))
    except ValueError:
        return None
    if provider != "Ashby" or posted_board.casefold() != board.casefold() or not job_id:
        return None
    return _result("Ashby", company, data.get("title"), f"https://{site}/{board}/{job_id}",
                   _text(data.get("descriptionPlain")) or _html_text(data.get("descriptionHtml")),
                   data.get("location"), profile)


def _request_board(provider, api, board, job, deadline):
    if provider == "Greenhouse":
        path = f"/v1/boards/{board}/jobs"
        if job:
            return [_get_json(api, f"{path}/{job}", deadline)]
        payload = _get_json(api, path + "?content=true", deadline)
    elif provider == "Lever":
        path = f"/v0/postings/{board}"
        if job:
            return [_get_json(api, f"{path}/{job}?mode=json", deadline)]
        payload = _get_json(api, path + "?mode=json", deadline)
    else:
        payload = _get_json(api, f"/posting-api/job-board/{board}", deadline)
    if provider != "Lever":
        payload = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(payload, list):
        raise ValueError("The job provider returned an unexpected response.")
    return payload


def _read_results(parsed, profile, deadline, search, single):
    provider, api, site, board, job_id = parsed
    data = _request_board(provider, api, board, job_id if single else "", deadline)
    company = board.replace("-", " ").replace("_", " ")
    if provider == "Greenhouse" and data and not (single and isinstance(data[0], dict) and data[0].get("company_name")):
        try:
            info = _get_json(api, f"/v1/boards/{board}", min(deadline, time.monotonic() + 3))
            if isinstance(info, dict):
                company = _text(info.get("name")) or company
        except ValueError:
            pass  # Listings remain useful if board branding is unavailable.
    formatter = {"Greenhouse": _greenhouse, "Lever": _lever, "Ashby": _ashby}[provider]
    words = search.casefold().split()
    seen = set()
    results = []
    for row in data:
        if not isinstance(row, dict) or not single and row.get("isListed") is False:
            continue
        if not single and provider == "Greenhouse" and "internal_job_id" in row and row["internal_job_id"] is None:
            continue  # Greenhouse includes general expressions of interest on some boards.
        result = formatter(row, site, board, company, profile)
        if result is None:
            continue
        url = result["posting_url"]
        if single:
            if url.rsplit("/", 1)[-1] == job_id:
                return result
            continue
        if url in seen:
            continue
        searchable = " ".join(result[field] for field in ("title", "location", "description")).casefold()
        if all(word in searchable for word in words):
            results.append(result)
            seen.add(url)
    if single:
        raise ValueError(_UNAVAILABLE)
    results.sort(key=lambda item: (not item["match"]["target_role_match"], -len(item["match"]["matched"]),
                                   item["title"].casefold()))
    return results[:_RESULT_LIMIT]


def _lookup(parsed, profile, *, search="", single=False):
    # An OS DNS lookup can outlast a socket timeout. Limit how long the local
    # browser waits and how many background lookups can outlive their request.
    if not _REQUESTS.acquire(blocking=False):
        raise ValueError("Several job lookups are already running. Try again in a moment.")
    result = queue.SimpleQueue()
    deadline = time.monotonic() + _LOOKUP_TIMEOUT

    def run():
        try:
            result.put(_read_results(parsed, profile, deadline, search, single))
        except Exception as exc:
            result.put(exc)
        finally:
            _REQUESTS.release()

    try:
        threading.Thread(target=run, daemon=True, name="public-job-lookup").start()
    except RuntimeError:
        _REQUESTS.release()
        raise
    try:
        value = result.get(timeout=_LOOKUP_TIMEOUT)
    except queue.Empty:
        raise ValueError("The job provider took too long to respond. Try again shortly.") from None
    if isinstance(value, Exception):
        raise value
    return value


def import_posting(url, profile):
    parsed = _parse(url)
    if not parsed[-1]:
        raise ValueError("Paste the link to an individual job. Use company board search to browse open roles.")
    return _lookup(parsed, profile, single=True)


def discover_board(url, profile, query=""):
    if not isinstance(query, str) or len(query) > 150:
        raise ValueError("Enter up to 150 characters of job or location keywords.")
    return _lookup(_parse(url), profile, search=query)
