"""Find attributable company context on the recipient's public HTTPS site."""

import http.client
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


PERSONAL_DOMAINS = set(
    "gmail.com googlemail.com yahoo.com ymail.com rocketmail.com outlook.com hotmail.com "
    "live.com msn.com icloud.com me.com mac.com aol.com proton.me protonmail.com pm.me "
    "fastmail.com hey.com mail.com gmx.com gmx.net gmx.de web.de tutanota.com tuta.com "
    "yandex.com yandex.ru mail.ru rambler.ru zoho.com rediffmail.com qq.com 163.com "
    "126.com sina.com inbox.com comcast.net att.net verizon.net sbcglobal.net "
    "btinternet.com orange.fr free.fr laposte.net libero.it naver.com hanmail.net "
    "daum.net bk.ru list.ru inbox.ru shaw.ca bell.net bigpond.com outlook.in "
    "users.noreply.github.com privaterelay.appleid.com mozmail.com duck.com".split()
)
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_BOILERPLATE = re.compile(
    r"cookie|all rights reserved|privacy policy|terms of (use|service)|javascript|"
    r"domain (is |may be |name )?(for sale|parked)|buy this domain|buy this website|"
    r"under construction|coming soon|cannot be (found|displayed)|access denied|"
    r"the official (home|website) of|copyright\s*©|privacy notice|looking for work\?|"
    r"web server is working|default web (site|page)|sign in to continue|"
    r"checking your browser|captcha|skip to (main )?content|enable scripts|"
    r"ignore (all |any |previous |prior )*instructions|system prompt|"
    r"verification (code|token)|password|unsubscribe|https?://|[<>@{}]", re.I,
)
_MAX_BYTES = 384 * 1024
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_GENERIC = set(
    "a an and at are as be best better build business businesses by can company day doing "
    "every for forward from future goal goals great grow growth help helps home homepage in "
    "innovative innovation is it leader leading more next of official on one organization "
    "organizations our people platform products provide provides quality results services "
    "site solutions succeed success team teams technology the their them they this thrive to "
    "today tomorrow transform transforming trusted us way we website welcome with work world "
    "you your achieve dreams potential reach unlock".split()
)


class ResearchError(ValueError):
    """A usable public source could not be established."""


def recipient_domain(email):
    email = str(email or "").strip()
    if len(email) > 254 or email.count("@") != 1:
        raise ResearchError("Enter a valid recipient email address.")
    local, domain = email.rsplit("@", 1)
    if (not local or len(local) > 64 or local.startswith(".") or local.endswith(".")
            or ".." in local or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)):
        raise ResearchError("Enter a valid recipient email address.")
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ResearchError("Enter a valid email domain.") from exc
    if not _public_hostname(domain):
        raise ResearchError("Enter an email address with a public domain.")
    return domain


def shared_domain(domain):
    return (domain in PERSONAL_DOMAINS
            or bool(re.fullmatch(r"(yahoo|hotmail|outlook|live|yandex|gmx)\.[a-z.]+", domain)))


def _public_hostname(host):
    labels = host.split(".")
    return (len(host) <= 253 and len(labels) >= 2 and all(_LABEL.fullmatch(s) for s in labels)
            and not labels[-1].isdigit() and labels[-1] not in
            {"local", "localhost", "internal", "lan", "test", "invalid", "example", "onion"})


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ResearchError("The company website took too long to respond.")
    return seconds


def _addresses(host, deadline):
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.put(exc)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        choices = result.get(timeout=_remaining(deadline))
    except queue.Empty as exc:
        raise ResearchError("The company domain did not resolve in time.") from exc
    if isinstance(choices, Exception):
        raise ResearchError("The company domain could not be resolved.") from choices
    for _, _, _, _, address in choices:
        ip = ipaddress.ip_address(address[0])
        # Some networks supply DNS64 addresses even for IPv4-only public sites.
        if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
            ip = ipaddress.IPv4Address(ip.packed[-4:])
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour or ip.teredo)):
            raise ResearchError("Company research requires a public website.")
    return choices


def _fetch(url, domain, deadline):
    for _ in range(4):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or host not in {domain, "www." + domain}
                or not _public_hostname(host)):
            raise ResearchError("The company site redirected outside its verified domain.")
        choices = _addresses(host, deadline)
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        # Connect to the verified numeric address. TLS still verifies the actual hostname.
        connection = http.client.HTTPSConnection(host, timeout=_remaining(deadline), context=context)
        timer = None
        try:
            for family, kind, protocol, _, address in choices:
                sock = socket.socket(family, kind, protocol)
                try:
                    sock.settimeout(min(2.5, _remaining(deadline)))
                    sock.connect(address)
                    sock.settimeout(_remaining(deadline))
                    connection.sock = context.wrap_socket(sock, server_hostname=host)
                    break
                except OSError:
                    sock.close()
            if connection.sock is None:
                raise ResearchError("The public company website could not be reached over HTTPS.")
            timer = threading.Timer(_remaining(deadline), _end_request, args=(connection.sock,))
            timer.daemon = True
            timer.start()
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={
                "User-Agent": "EmailSender/1.0 (company context lookup)",
                "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity",
            })
            with connection.getresponse() as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise ResearchError("The company website returned an incomplete redirect.")
                    url = urljoin(url, location)
                    continue
                if response.status != 200 or response.headers.get_content_type() not in {
                    "text/html", "application/xhtml+xml"
                }:
                    raise ResearchError("The company website did not return a readable page.")
                data = bytearray()
                while len(data) < _MAX_BYTES:
                    _remaining(deadline)
                    chunk = response.read1(min(32768, _MAX_BYTES - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                reader = _PageReader()
                charset = response.headers.get_content_charset() or "utf-8"
                reader.feed(data.decode(charset, "replace"))
                return reader, url
        finally:
            if timer:
                timer.cancel()
            connection.close()
    raise ResearchError("The company website returned too many redirects.")


def _end_request(sock):
    # Socket timeouts alone reset on each read and do not bound slow header streams.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _PageReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.company = ""
        self.descriptions = []
        self.paragraphs = []
        self.links = []
        self.ignore = 0
        self.paragraph = None
        self.anchor = None
        self.href = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "template"}:
            self.ignore += 1
        elif self.ignore:
            return
        elif tag == "meta":
            field = (attrs.get("property") or attrs.get("name") or "").lower()
            content = " ".join((attrs.get("content") or "").split())
            if field in {"description", "og:description", "twitter:description"} and content:
                self.descriptions.append(content)
            elif field == "og:site_name" and content:
                self.company = content
        elif tag == "p":
            self.paragraph = []
        elif tag == "a":
            self.href = attrs.get("href") or ""
            self.anchor = []

    def handle_data(self, data):
        if not self.ignore:
            if self.paragraph is not None:
                self.paragraph.append(data)
            if self.anchor is not None:
                self.anchor.append(data)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.ignore = max(0, self.ignore - 1)
        elif tag == "p" and self.paragraph is not None:
            self.paragraphs.append(" ".join("".join(self.paragraph).split()))
            self.paragraph = None
        elif tag == "a" and self.anchor is not None:
            self.links.append((" ".join("".join(self.anchor).split()), self.href))
            self.anchor = None


def _keywords(text):
    return set(re.findall(r"[\w+#-]{3,}", text.lower())) - _GENERIC


def _snippet(page, focus=""):
    matches = []
    keywords = _keywords(focus)
    for candidate in page.descriptions + page.paragraphs:
        if _BOILERPLATE.search(candidate) or any(ord(c) < 32 for c in candidate):
            continue
        line = re.split(r"(?<=[.!?])\s+", candidate, maxsplit=1)[0]
        words = line.split()
        if len(words) < 6 or len(set(w.lower() for w in words)) < 5:
            continue
        excerpt = " ".join(words[:22]).rstrip(".,;:!?")
        facts = _keywords(excerpt)
        if not facts:
            continue
        matches.append((len(facts & keywords), excerpt + ("…" if len(words) > 22 else "")))
    # For equally relevant passages, preserve the site's own description priority.
    return max(matches, key=lambda match: match[0])[1] if matches else ""


def _job_link(page, base, domain, target):
    def same_domain(href):
        link = urlsplit(urljoin(base, href))
        return (link.scheme == "https" and link.hostname in {domain, "www." + domain}
                and link.username is None and link.password is None and link.port in (None, 443))

    for title, href in page.links:
        if (href and not href.startswith(("#", "?")) and title.casefold() == target.casefold()
                and same_domain(href)):
            return urljoin(base, href)
    return ""


def lookup(domain, target_role="", focus=""):
    """Read the company page, optionally finding an exact matching official role link."""
    if shared_domain(domain) or not _public_hostname(domain):
        raise ResearchError("Add the company and one observation for this shared email domain.")
    deadline = time.monotonic() + 9
    try:
        home, base = _fetch("https://" + domain + "/", domain, deadline)
        company = home.company
        if len(company) > 70 or re.search(r"[<>@{}]|https?://", company):
            company = ""
        job_url = ""
        if target_role:
            job_url = _job_link(home, base, domain, target_role)
            if not job_url:
                for label in ("careers", "jobs", "open roles", "open positions", "join us"):
                    careers = _job_link(home, base, domain, label)
                    if careers:
                        listing, location = _fetch(careers, domain, deadline)
                        job_url = _job_link(listing, location, domain, target_role)
                        break
        return {"company": company or domain, "snippet": _snippet(home, focus),
                "source": base, "job_url": job_url}
    except ResearchError:
        raise
    except (OSError, ValueError, LookupError, http.client.HTTPException) as exc:
        raise ResearchError("The company website could not supply usable context.") from exc
