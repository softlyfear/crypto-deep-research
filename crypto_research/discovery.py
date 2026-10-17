"""Public HTML discovery; search results and links are unverified leads, not facts."""

from html.parser import HTMLParser
import ipaddress
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit


def lead_url(value, base):
    if value.strip().startswith("#"):
        return None
    try:
        url = urljoin(base, value.strip())
        parts = urlsplit(url)
        if parts.hostname in {"duckduckgo.com", "html.duckduckgo.com"} and parts.path.startswith("/l/"):
            url = parse_qs(parts.query).get("uddg", [""])[0]
            parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.port not in (None, 443) or parts.hostname == "localhost"
                or parts.hostname.endswith(".localhost")):
            return None
        try:
            if not ipaddress.ip_address(parts.hostname).is_global:
                return None
        except ValueError:
            pass
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return None


class Anchors(HTMLParser):
    def __init__(self, *, results_only=False):
        super().__init__(convert_charrefs=True)
        self.results_only = results_only
        self.links = []
        self.current = None
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored or tag != "a":
            return
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if attrs.get("href") and (not self.results_only or "result__a" in classes):
            self.current = [attrs["href"], []]

    def handle_data(self, text):
        if self.current is not None and not self.ignored:
            self.current[1].append(text)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "a" and self.current is not None:
            self.links.append((self.current[0], " ".join("".join(self.current[1]).split())))
            self.current = None


def extract_links(text, base, *, results_only=False, query=None, limit=12):
    from .core import safe_url

    parser = Anchors(results_only=results_only)
    parser.feed(text)
    candidates = parser.links
    if not results_only:
        candidates += [(match[2], match[1]) for match in
                       re.finditer(r'(?<!!)\[([^\]\n]+)\]\(([^\s()]+)(?:\s+"[^"]*")?\)', text)]
    links, seen = [], set()
    for href, title in candidates:
        url = lead_url(href, base)
        if not url or url in seen or query and query.casefold() not in (title + " " + url).casefold():
            continue
        seen.add(url)
        links.append({"url": safe_url(url), "title": title[:300]})
    return {"untrusted_leads": links[:limit], "match_count": len(links), "truncated": len(links) > limit}
