"""Evidence storage and calculations."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import fcntl
import hashlib
from http.client import HTTPSConnection
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import tempfile
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


MAX_BODY = 16 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30
KINDS = ("official", "aggregator", "explorer", "rpc", "other", "search")
RPC_METHODS = {"eth_chainId", "eth_blockNumber", "eth_getBalance", "eth_getCode", "eth_getBlockByNumber"}
RPC_BLOCK_QUANTITIES = {"/result/number", "/result/timestamp", "/result/gasUsed", "/result/gasLimit",
                        "/result/baseFeePerGas", "/result/size"}
PROFILES = {"deep": {"max_requests": 96, "minutes": 60}, "quick": {"max_requests": 24, "minutes": 15}}
VERIFICATION_NOTICE = "Verification: no confirmed independent cross-model review."
API_SETTINGS = (
    ("COINGECKO_DEMO_API_KEY", "CoinGecko Demo", "free", True),
    ("CONTEXT7_API_KEY", "Context7", "free", False),
    ("CR_RPC_URL", "EVM RPC", "basic", True),
    ("EXA_API_KEY", "Exa", "optional", False),
    ("TAVILY_API_KEY", "Tavily", "optional", False),
    ("FIRECRAWL_API_KEY", "Firecrawl", "optional", False),
    ("DUNE_API_KEY", "Dune", "optional", False),
    ("THEGRAPH_API_KEY", "The Graph", "optional", False),
    ("ALCHEMY_API_KEY", "Alchemy", "optional", False),
    ("ETHERSCAN_API_KEY", "Etherscan", "optional", False),
    ("MOBULA_API_KEY", "Mobula", "optional", False),
    ("CRYPTORANK_API_KEY", "CryptoRank", "paid", False),
    ("GOLDRUSH_API_KEY", "GoldRush", "paid", False),
    ("COINGECKO_PRO_API_KEY", "CoinGecko Pro", "paid", False),
)


class ResearchError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().isoformat()


def setting_value(name):
    value = os.environ.get(name, "")
    if not value.strip():
        return ""
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ResearchError("Invalid " + name + ": whitespace or control characters")
    if name == "CR_RPC_URL":
        valid = False
        try:
            parts = urlsplit(value)
            valid = (parts.scheme == "https" and bool(parts.hostname)
                     and not parts.username and not parts.password and not parts.fragment
                     and parts.port in (None, 443))
            if valid:
                try:
                    valid = ipaddress.ip_address(parts.hostname).is_global
                except ValueError:
                    valid = parts.hostname != "localhost" and not parts.hostname.endswith(".localhost")
        except ValueError:
            pass
        if not valid:
            raise ResearchError("Invalid CR_RPC_URL: expected a public HTTPS URL on port 443 without userinfo or fragment")
    return value


def preflight():
    """Check configuration offline; never return credential values."""
    settings, warnings = [], [
        "Presence and syntax only; authentication, quotas, DNS and network availability are not verified.",
        "Keys do not authorize paid requests or enable integrations; MCP authentication is managed separately in OpenCode.",
    ]
    for name, service, group, supported in API_SETTINGS:
        configured = bool(os.environ.get(name, "").strip())
        try:
            value = setting_value(name)
            value_status = "present" if value else "missing"
        except ResearchError as error:
            value_status = "invalid"
            warnings.append(str(error))
        usable = supported and value_status != "invalid" and (configured or name == "COINGECKO_DEMO_API_KEY")
        if value_status == "invalid":
            state = "invalid"
        elif not supported:
            state = "not_integrated"
            if configured:
                warnings.append(name + " is present, but its authenticated integration is not implemented")
        elif configured:
            state = "configured"
        else:
            state = "keyless" if name == "COINGECKO_DEMO_API_KEY" else "missing"
        settings.append({"environment": name, "service": service, "group": group,
                         "configured": configured, "value_status": value_status, "status": state,
                         "adapter_available": supported, "usable_by_local_adapter": usable})
    return {"checked_at": stamp(), "check_type": "local_configuration_only", "network_checked": False,
            "settings": settings, "public_sources": ["DefiLlama", "Official public HTTPS pages"],
            "warnings": warnings}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked(run):
    run = Path(run).resolve()
    if not (run / "run.json").is_file():
        raise ResearchError("Not a research run: missing run.json")
    with (run / ".lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield run


def no_symlinks(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ResearchError("Symlink paths are not allowed")


def run_directory(value, root):
    base = root / "artifacts" / "runs"
    no_symlinks(base)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    no_symlinks(candidate)
    directory = candidate.resolve()
    if directory == base or not directory.is_relative_to(base):
        raise ResearchError("Run must be strictly below cwd/artifacts/runs")
    if not (directory / "run.json").is_file():
        raise ResearchError("Not a research run: missing run.json")

    def walk_error(_error):
        raise ResearchError("Cannot validate research run contents")

    for parent, directories, files in os.walk(directory, followlinks=False, onerror=walk_error):
        for name in directories + files:
            if (Path(parent) / name).is_symlink():
                raise ResearchError("Symlinks inside research runs are not allowed")
    return directory


def init(query, *, root="artifacts/runs", mode="both", max_requests=None, minutes=None, profile="deep"):
    if profile not in PROFILES:
        raise ResearchError("Unknown research profile")
    max_requests = PROFILES[profile]["max_requests"] if max_requests is None else max_requests
    minutes = PROFILES[profile]["minutes"] if minutes is None else minutes
    if not query.strip() or not 1 <= max_requests <= 1000 or not 1 <= minutes <= 120:
        raise ResearchError("Query required; requests must be 1..1000 and minutes 1..120")
    configuration = preflight()
    created = now()
    run_id = created.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run = Path(root).resolve() / run_id
    (run / "evidence").mkdir(parents=True)
    save(run / "run.json", {
        "schema_version": 1, "run_id": run_id, "query": query,
        "created_at": created.isoformat(), "deadline_at": (created + timedelta(minutes=minutes)).isoformat(),
        "mode": mode, "language": "ru", "budget": {"max_requests": max_requests, "requests_started": 0},
        "profile": profile, "coverage_schema": 1,
        "identity": {"status": "unresolved"},
        "preflight": configuration,
    })
    save(run / "evidence/index.json", [])
    save(run / "metrics.json", [])
    return {"run_dir": str(run), "run_id": run_id, "preflight": configuration}


def public_url(url):
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ResearchError("Only public HTTPS URLs without embedded credentials are supported")
        if parts.fragment:
            raise ResearchError("Remove URL fragments before fetching")
        if parts.port not in (None, 443):
            raise ResearchError("Only HTTPS port 443 is supported")
        addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    except (ValueError, OSError) as error:
        raise ResearchError("Invalid or unresolvable public HTTPS URL") from error
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ResearchError("Private, loopback, link-local and reserved destinations are not allowed")
    return url


def safe_url(url, *, rpc=False):
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    # RPC paths and queries may contain keys.
    if rpc:
        return urlunsplit((parts.scheme, netloc, "/[endpoint]", "", ""))
    secret_words = ("key", "token", "secret", "password", "signature", "auth", "credential")
    query = [(key, "[redacted]" if any(word in key.lower() for word in secret_words) else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


class PublicRedirects(HTTPRedirectHandler):
    def __init__(self, before_request=None):
        super().__init__()
        self.before_request = before_request

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        if urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            if any("key" in key.lower() or "authorization" in key.lower() for key in req.headers):
                raise ResearchError("Refusing cross-host redirect of an authenticated request")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and self.before_request is not None:
            self.before_request()
        return redirected


def connect_public(address, timeout, source_address=None):
    """Connect to a validated numeric sockaddr, without a second DNS lookup."""
    host, port = address
    if port != 443:
        raise ResearchError("Only HTTPS port 443 is supported")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ResearchError("Private, loopback, link-local and reserved destinations are not allowed")
    last_error = None
    deadline = monotonic() + timeout
    for family, kind, protocol, _, sockaddr in addresses:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ResearchError("Source download timed out")
        connection = None
        try:
            connection = socket.socket(family, kind, protocol)
            connection.settimeout(remaining)
            if source_address:
                connection.bind(source_address)
            connection.connect(sockaddr)
            return connection
        except OSError as error:
            last_error = error
            if connection is not None:
                connection.close()
    raise last_error


class PublicHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Preserve the original host for TLS verification and SNI.
        self._create_connection = connect_public


class PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(PublicHTTPSConnection, request, context=self._context)


def download(url, *, headers=None, payload=None, before_request=None):
    deadline = monotonic() + DOWNLOAD_TIMEOUT

    def remaining():
        seconds = deadline - monotonic()
        if seconds <= 0:
            raise ResearchError("Source download timed out")
        return min(20, seconds)

    def redirect():
        remaining()
        if before_request is not None:
            before_request()

    public_url(url)
    request = Request(url, data=payload, headers={"User-Agent": "crypto-deep-research/0.1", **(headers or {})})
    with build_opener(ProxyHandler({}), PublicRedirects(redirect), PublicHTTPSHandler()).open(request, timeout=remaining()) as response:
        body = bytearray()
        while True:
            remaining()
            chunk = response.read1(min(64 * 1024, MAX_BODY + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise ResearchError("Source response exceeds 16 MiB; use a narrower endpoint")
        return bytes(body), response.headers.get("Content-Type", ""), response.status, response.geturl()


def reserve_request(run):
    with locked(run) as directory:
        manifest = load(directory / "run.json")
        if now() >= datetime.fromisoformat(manifest["deadline_at"]):
            raise ResearchError("Collection deadline reached; finish using existing evidence and disclose gaps")
        budget = manifest["budget"]
        if budget["requests_started"] >= budget["max_requests"]:
            raise ResearchError("Request budget exhausted; failed requests and redirects also consume the budget")
        budget["requests_started"] += 1
        save(directory / "run.json", manifest)


def fetch(run, url, *, kind="other", label="", headers=None, payload=None, ttl=300):
    if kind not in KINDS or ttl < 0:
        raise ResearchError("Invalid source kind or cache TTL")
    # Hash headers into the cache key; don't store credentials.
    cache_key = hashlib.sha256(json.dumps([url, kind, headers or {}, (payload or b"").hex()], sort_keys=True).encode()).hexdigest()
    with locked(run) as directory:
        index = load(directory / "evidence/index.json")
        for item in reversed(index):
            if item["cache_key"] == cache_key and item["status"] == "ok":
                if (now() - datetime.fromisoformat(item["retrieved_at"])).total_seconds() < ttl:
                    return {**item, "cache_hit": True}
    reserve_request(directory)
    record = {"id": "ev_" + uuid4().hex[:12], "url": safe_url(url, rpc=kind == "rpc"),
              "kind": kind, "label": label, "cache_key": cache_key, "retrieved_at": stamp(), "status": "error"}
    if kind == "rpc" and payload:
        record["rpc_request"] = json.loads(payload)
    try:
        body, content_type, http_status, final_url = download(url, headers=headers, payload=payload,
                                                           before_request=lambda: reserve_request(directory))
        record.update(http_status=http_status, content_type=content_type,
                      final_url=safe_url(final_url, rpc=kind == "rpc"), sha256=hashlib.sha256(body).hexdigest())
        suffix = ".json" if "json" in content_type.lower() else ".txt"
        record["body_path"] = "evidence/" + record["id"] + suffix
        (directory / record["body_path"]).write_bytes(body)
        # HTTP 200 can still contain a JSON-RPC error.
        if kind == "rpc":
            decoded = json.loads(body)
            if not isinstance(decoded, dict) or decoded.get("error") is not None or "result" not in decoded:
                raise ResearchError("RPC response has no successful result")
        record["status"] = "ok"
    except HTTPError as error:
        record.update(status="error", http_status=error.code, error="HTTP error; check availability, rate limits and optional key")
    except (URLError, TimeoutError, OSError, ResearchError, ValueError) as error:
        # Exception text may expose URL credentials.
        record["status"] = "error"
        record["error"] = str(error) if isinstance(error, ResearchError) else "Fetch failed: " + type(error).__name__
    record["retrieved_at"] = stamp()
    with locked(directory):
        index = load(directory / "evidence/index.json")
        index.append(record)
        save(directory / "evidence/index.json", index)
    return record


def coingecko_headers():
    key = setting_value("COINGECKO_DEMO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def slug(value):
    if not value or len(value) > 120 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise ResearchError("Use a canonical lowercase source ID, not a ticker or URL")
    return value


def discover(run, query):
    return {"coingecko": fetch(run, "https://api.coingecko.com/api/v3/search?" + urlencode({"query": query}),
                              kind="aggregator", label="CoinGecko candidates, NOT confirmed identity", headers=coingecko_headers()),
             "defillama": fetch(run, "https://api.llama.fi/protocols", kind="aggregator", label="DefiLlama protocol directory")}


def search(run, query, *, limit=8):
    """Submit the public no-JavaScript HTML search form; no API key or paid service."""
    from .discovery import extract_links

    if not isinstance(query, str) or not query.strip() or len(query) > 500 or not 1 <= limit <= 12:
        raise ResearchError("Search needs a public query of 1..500 characters and limit 1..12")
    record = fetch(run, "https://html.duckduckgo.com/html/", kind="search",
                   label="Search discovery only: " + query, payload=urlencode({"q": query}).encode(),
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    if record["status"] != "ok":
        return {"status": "error", "evidence": record, "untrusted_leads": [], "warning": "Search unavailable, not evidence of absence"}
    text = evidence_body(run, record).decode("utf-8", errors="replace")
    results = extract_links(text, record.get("final_url", record["url"]), results_only=True, limit=limit)
    return {"status": "ok" if results["match_count"] else "unavailable", "evidence": record, **results,
            "warning": "Unverified leads only; fetch and read original sources. Empty results or bot challenges do not prove absence."}


def protocol(run, identifier):
    return fetch(run, "https://api.llama.fi/protocol/" + slug(identifier), kind="aggregator", label="DefiLlama protocol detail")


def market(run, identifier):
    return fetch(run, "https://api.coingecko.com/api/v3/coins/" + slug(identifier) +
                 "?localization=false&tickers=false&community_data=false&developer_data=false",
                 kind="aggregator", label="CoinGecko token snapshot", headers=coingecko_headers())


def fees(run, identifier, metric="fees"):
    data_type = {"fees": "dailyFees", "revenue": "dailyRevenue"}[metric]
    return fetch(run, "https://api.llama.fi/summary/fees/" + slug(identifier) + "?" +
                 urlencode({"dataType": data_type, "excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"}),
                 kind="aggregator", label="DefiLlama " + metric + "; verify adapter methodology and period")


def rpc(run, method, params):
    if method not in RPC_METHODS or not isinstance(params, list):
        raise ResearchError("Only whitelisted read-only JSON-RPC methods with array params are allowed")
    if method in {"eth_chainId", "eth_blockNumber"} and params:
        raise ResearchError("This RPC method takes no params")
    if method == "eth_getBlockByNumber" and (len(params) != 2 or params[1] is not False):
        raise ResearchError("Block snapshots must use [block, false], without full transactions")
    if method in {"eth_getBalance", "eth_getCode"}:
        if len(params) != 2 or not isinstance(params[0], str) or len(params[0]) != 42:
            raise ResearchError("Expected [0x-prefixed 20-byte address, block]")
        try:
            if not params[0].startswith("0x"):
                raise ValueError()
            int(params[0][2:], 16)
        except ValueError as error:
            raise ResearchError("Invalid EVM address") from error
    endpoint = setting_value("CR_RPC_URL")
    if not endpoint:
        raise ResearchError("Set CR_RPC_URL to a public HTTPS RPC for the confirmed project chain")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    return fetch(run, endpoint, kind="rpc", label="Read-only RPC: " + method,
                 headers={"Content-Type": "application/json"}, payload=payload, ttl=30)


def evidence_body(run, record):
    directory = Path(run).resolve()
    path = (directory / record["body_path"]).resolve()
    if not path.is_relative_to(directory / "evidence"):
        raise ResearchError("Evidence path escapes evidence directory")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != record["sha256"]:
        raise ResearchError("Evidence hash mismatch: stored source was modified")
    return body


def pointer(document, path):
    if path == "":
        return document
    if not path.startswith("/"):
        raise ResearchError("JSON pointer must be empty or start with /")
    try:
        for part in path[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(document, list):
                if not re.fullmatch(r"0|[1-9]\d*", part):
                    raise ResearchError("JSON array pointers require canonical nonnegative indices")
                document = document[int(part)]
            else:
                document = document[part]
        return document
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ResearchError("JSON pointer does not resolve") from error


def number(value):
    if isinstance(value, bool) or value is None:
        raise ResearchError("Missing or boolean data is not a numeric observation")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ResearchError("Observation must be a decimal number") from error
    if not result.is_finite() or abs(result.adjusted()) > 100:
        raise ResearchError("Non-finite or unreasonably large numeric value")
    return result


NUMBER_WORDS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split(),
    range(21),
))
NUMBER_ATOM = "|".join((*NUMBER_WORDS, "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
                        "hundred", "thousand", "million", "billion", "trillion"))
WORD_NUMBER = re.compile(r"(?<![\w-])(?:(?:minus|negative|plus|positive)\s+)?(?:" + NUMBER_ATOM + r")(?:(?:[\s-]+)(?:(?:and|point)\s+)?(?:" + NUMBER_ATOM + r"))*(?![\w-])", re.IGNORECASE)
TEXT_NUMBER = re.compile(r"(?<![\w.,])[+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\d.eE])")
GROUPED_NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?(?:[eE][+-]?\d+)?|[+-]?\.\d+(?:[eE][+-]?\d+)?")


def text_number(quote, literal):
    tokens = []
    for match in TEXT_NUMBER.finditer(quote):
        token = match.group()
        if token.endswith(","):
            token = token[:-1]
        if GROUPED_NUMBER.fullmatch(token):
            tokens.append(token)
            # "1,234." may end a sentence rather than carry a decimal point.
            if token.endswith(".") and GROUPED_NUMBER.fullmatch(token[:-1]):
                tokens.append(token[:-1])
    if literal in tokens:
        return number(literal.replace(",", ""))
    if literal in (match.group() for match in WORD_NUMBER.finditer(quote)) and literal.casefold() in NUMBER_WORDS:
        return number(NUMBER_WORDS[literal.casefold()])
    raise ResearchError("Numeric literal does not occur as a full supported number in the quote")


def observation_value(record, body, path, *, quote=None, literal=None):
    if record.get("kind") == "search":
        raise ResearchError("Search results are discovery leads; fetch original sources before recording observations")
    if quote is not None:
        if path != "" or not isinstance(quote, str) or not quote or len(quote) > 2000 or literal is None:
            raise ResearchError("Text observations need an empty pointer, exact source quote and numeric literal")
        if quote not in body.decode("utf-8", errors="replace"):
            raise ResearchError("Quoted text does not occur in the saved source")
        return text_number(quote, literal)
    if literal is not None:
        raise ResearchError("A numeric literal requires its exact source quote")
    document = json.loads(body, parse_float=Decimal)
    value = pointer(document, path)
    method = record.get("rpc_request", {}).get("method")
    if (method in {"eth_chainId", "eth_blockNumber", "eth_getBalance"} and path == "/result"
            or method == "eth_getBlockByNumber" and path in RPC_BLOCK_QUANTITIES):
        if not isinstance(value, str) or not value.startswith("0x"):
            raise ResearchError("Numeric EVM RPC result must be hex-encoded")
        try:
            value = int(value, 16)
        except ValueError as error:
            raise ResearchError("Invalid numeric EVM RPC result") from error
    return number(value)


def source_metadata(record):
    if isinstance(record.get("http_status"), int) and record["http_status"] >= 400:
        reason = "HTTP error"
    elif record.get("error") in {"Fetch failed: TimeoutError", "Source download timed out"}:
        reason = "Source download timed out"
    else:
        reason = "Source request failed"
    return {"evidence_id": record["id"], "status": record["status"], "kind": record["kind"],
            "url": safe_url(record["url"], rpc=record["kind"] == "rpc"),
            "retrieved_at": record["retrieved_at"], "http_status": record.get("http_status"),
            "error": reason, "body_inspected": False}


def inspect_source(run, evidence_id, path="", *, limit=8, query=None, links=False):
    if not 1 <= limit <= 12:
        raise ResearchError("Preview limit must be 1..12")
    with locked(run) as directory:
        record = next((item for item in load(directory / "evidence/index.json") if item["id"] == evidence_id), None)
        if not record:
            raise ResearchError("Unknown evidence ID")
        if record["status"] != "ok":
            if path or query or links:
                raise ResearchError("Failed evidence supports metadata inspection only")
            return source_metadata(record)
        body = evidence_body(directory, record)
    if links:
        if path or record["kind"] == "rpc":
            raise ResearchError("Link discovery requires a saved page, without a JSON pointer or RPC")
        from .discovery import extract_links
        return {"evidence_id": evidence_id, **extract_links(body.decode("utf-8", errors="replace"),
                record.get("final_url", record["url"]), query=query, limit=limit)}
    try:
        document = json.loads(body, parse_float=Decimal)
    except ValueError:
        if path:
            raise ResearchError("Pointers require JSON evidence; use --query for text search")
        if query:
            text = body.decode("utf-8", errors="replace")
            matches, count = [], 0
            for match in re.finditer(re.escape(query), text, re.IGNORECASE):
                count += 1
                if len(matches) < limit:
                    start = max(0, match.start() - 160)
                    matches.append({"offset": match.start(), "text": text[start:min(match.end() + 320, start + 600)]})
            return {"evidence_id": evidence_id, "match_count": count,
                    "untrusted_matches": matches, "truncated": count > len(matches)}
        return {"evidence_id": evidence_id, "untrusted_source_preview": body[:8000].decode("utf-8", errors="replace"), "truncated": len(body) > 8000}
    selected = pointer(document, path)

    def preview(value, depth=0):
        if isinstance(value, (dict, list)):
            if depth >= 2:
                return {"type": type(value).__name__, "length": len(value)}
            if isinstance(value, dict):
                return {key: preview(item, depth + 1) for key, item in list(value.items())[:limit]}
            return [preview(item, depth + 1) for item in value[:limit]]
        if isinstance(value, Decimal):
            return str(value)
        return value[:300] if isinstance(value, str) else value

    if query:
        if not isinstance(selected, list):
            raise ResearchError("Query filtering requires an array, usually a protocol directory")
        matches = [(index, item) for index, item in enumerate(selected) if isinstance(item, dict) and
                   any(query.casefold() in str(item.get(key, "")).casefold() for key in ("id", "name", "slug", "symbol"))]
        return {"evidence_id": evidence_id, "match_count": len(matches), "untrusted_items": [
            {"pointer": path + "/" + str(index), "value": preview(item)} for index, item in matches[:limit]]}
    return {"evidence_id": evidence_id, "pointer": path, "type": type(selected).__name__,
            "length": len(selected) if isinstance(selected, (dict, list)) else None,
            "keys": list(selected) if isinstance(selected, dict) else None, "untrusted_preview": preview(selected)}


def observe(run, evidence_id, path, *, label, unit, as_of=None, quote=None, literal=None):
    with locked(run) as directory:
        record = next((item for item in load(directory / "evidence/index.json") if item["id"] == evidence_id), None)
        if not record or record["status"] != "ok":
            raise ResearchError("Observation requires successful saved evidence")
        try:
            value = observation_value(record, evidence_body(directory, record), path, quote=quote, literal=literal)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ResearchError("Observation source must be valid JSON") from error
        metric = {"id": "obs_" + uuid4().hex[:12], "type": "observation", "label": label, "unit": unit,
                  "value": str(value), "evidence_ids": [evidence_id], "json_pointer": path,
                  "as_of": as_of, "retrieved_at": record["retrieved_at"]}
        if quote is not None:
            metric.update(quote=quote, value_literal=literal)
        metrics = load(directory / "metrics.json")
        metrics.append(metric)
        save(directory / "metrics.json", metrics)
        return metric


def evaluate(expression, values):
    if len(expression) > 300:
        raise ResearchError("Formula too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ResearchError("Invalid formula") from error
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ResearchError("Formula too complex")
    used = set()

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name) and node.id in values:
            used.add(node.id)
            return number(values[node.id])
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return number(ast.get_source_segment(expression, node))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if not right:
                raise ResearchError("Division by zero")
            return left / right
        raise ResearchError("Only named inputs, decimal constants, + - * / and parentheses are allowed")

    with localcontext() as context:
        context.prec = 28
        result = number(visit(tree))
    if used != set(values):
        raise ResearchError("Every supplied input must be used in the formula")
    return str(result)


def calculate(run, expression, inputs, *, label, unit):
    if not isinstance(inputs, dict) or not inputs:
        raise ResearchError("Inputs must map formula names to saved metric IDs")
    with locked(run) as directory:
        metrics = load(directory / "metrics.json")
        by_id = {metric["id"]: metric for metric in metrics}
        if any(identifier not in by_id for identifier in inputs.values()):
            raise ResearchError("All calculation inputs must refer to saved metrics")
        result = evaluate(expression, {key: by_id[identifier]["value"] for key, identifier in inputs.items()})
        metric = {"id": "calc_" + uuid4().hex[:12], "type": "calculation", "label": label, "unit": unit,
                  "value": result, "expression": expression, "inputs": inputs,
                  "evidence_ids": sorted({source for identifier in inputs.values() for source in by_id[identifier]["evidence_ids"]}),
                  "created_at": stamp()}
        metrics.append(metric)
        save(directory / "metrics.json", metrics)
        return metric


def validate_identity(project, evidence):
    if not isinstance(project, dict) or project.get("status") != "resolved":
        raise ResearchError("Project identity has not been resolved")
    if project.get("token_status") not in {"unknown", "none_verified", "confirmed"} or not isinstance(project.get("name"), str) or not project["name"].strip():
        raise ResearchError("Invalid project identity")
    if not isinstance(project.get("chains"), list) or any(not isinstance(chain, str) or not chain.strip() for chain in project["chains"]):
        raise ResearchError("Identity chains must be an array of nonempty names")
    parts = urlsplit(project.get("official_url", ""))
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ResearchError("Official URL must be a public HTTPS page without credentials or query")
    evidence_ids = project.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(identifier, str) or identifier not in evidence or evidence[identifier]["status"] != "ok" for identifier in evidence_ids):
        raise ResearchError("Identity requires successful saved evidence")
    if not any(evidence[identifier]["kind"] == "official" and
               urlsplit(evidence[identifier].get("final_url", evidence[identifier]["url"])).hostname == parts.hostname
               for identifier in evidence_ids):
        raise ResearchError("Identity requires official evidence from the official URL host")


def identity(run, *, name, official_url, chains, token_status, evidence_ids):
    project = {"status": "resolved", "name": name, "official_url": official_url,
               "chains": chains, "token_status": token_status, "evidence_ids": evidence_ids}
    with locked(run) as directory:
        by_id = {item["id"]: item for item in load(directory / "evidence/index.json")}
        validate_identity(project, by_id)
        manifest = load(directory / "run.json")
        manifest["identity"] = project
        save(directory / "run.json", manifest)
        return manifest["identity"]


def status(run):
    with locked(run) as directory:
        manifest = load(directory / "run.json")
        evidence = load(directory / "evidence/index.json")
        return {**manifest, "run_dir": str(directory), "collection_open": now() < datetime.fromisoformat(manifest["deadline_at"]) and
                manifest["budget"]["requests_started"] < manifest["budget"]["max_requests"],
                "evidence_count": len(evidence), "failed_sources": [item["id"] for item in evidence if item["status"] != "ok"],
                "failed_source_details": [source_metadata(item) for item in evidence if item["status"] != "ok"],
                "metric_count": len(load(directory / "metrics.json"))}


def check(run, *, final=False, _snapshot=False):
    if _snapshot and not final:
        raise ResearchError("Publication requires a final check")
    errors, warnings = [], []
    snapshot = None
    with locked(run) as directory:
        manifest = load(directory / "run.json")
        records = load(directory / "evidence/index.json")
        evidence = {item["id"]: item for item in records}
        if len(evidence) != len(records):
            errors.append("Duplicate evidence IDs are not allowed")
        metrics = load(directory / "metrics.json")
        by_id = {}
        for record in records:
            if record["status"] != "ok":
                warnings.append("Unavailable source: " + record["id"])
            if record["status"] != "ok" and not record.get("body_path") and not record.get("sha256"):
                continue
            try:
                evidence_body(directory, record)
            except (ResearchError, OSError, KeyError) as error:
                errors.append(record["id"] + ": " + str(error))
        for metric in metrics:
            identifier = metric["id"]
            if identifier in by_id:
                errors.append("Duplicate metric ID: " + identifier)
            try:
                if not metric["evidence_ids"] or any(source not in evidence or evidence[source]["status"] != "ok" for source in metric["evidence_ids"]):
                    raise ResearchError("Unknown or failed evidence reference")
                if metric["type"] == "observation":
                    if len(metric["evidence_ids"]) != 1:
                        raise ResearchError("Observation must have exactly one source")
                    source = evidence[metric["evidence_ids"][0]]
                    if metric.get("retrieved_at") != source["retrieved_at"]:
                        raise ResearchError("Observation retrieval time differs from its source")
                    expected = str(observation_value(source, evidence_body(directory, source), metric["json_pointer"],
                                                     quote=metric.get("quote"), literal=metric.get("value_literal")))
                    if not metric.get("as_of"):
                        warnings.append("Observation period unknown: " + identifier)
                elif metric["type"] == "calculation":
                    expected = evaluate(metric["expression"], {key: by_id[value]["value"] for key, value in metric["inputs"].items()})
                    expected_sources = {source for value in metric["inputs"].values() for source in by_id[value]["evidence_ids"]}
                    if set(metric["evidence_ids"]) != expected_sources:
                        raise ResearchError("Calculation evidence does not match its inputs")
                else:
                    raise ResearchError("Unknown metric type")
                if number(expected) != number(metric["value"]):
                    raise ResearchError("Metric value does not reproduce")
            except (ResearchError, KeyError, ValueError, OSError) as error:
                errors.append(identifier + ": " + str(error))
            by_id[identifier] = metric
        if final:
            report_bytes = (directory / "report.md").read_bytes() if (directory / "report.md").is_file() else b""
            try:
                report_text = report_bytes.decode("utf-8")
            except UnicodeDecodeError:
                errors.append("Report must be valid UTF-8")
                report_text = report_bytes.decode("utf-8", errors="replace")

            def final_json(filename):
                # Agent-written artifacts may be malformed; report it instead of crashing.
                if not (directory / filename).is_file():
                    return None, False
                try:
                    return load(directory / filename), True
                except ValueError:
                    errors.append("Malformed final artifact: " + filename)
                    return None, False

            try:
                validate_identity(manifest.get("identity"), evidence)
            except (ResearchError, ValueError, TypeError) as error:
                errors.append("Identity: " + str(error))
            for filename in ("report.md", "claims.json", "verification.json"):
                if not (directory / filename).is_file():
                    errors.append("Missing final artifact: " + filename)
            if (directory / "report.md").is_file() and not report_text.strip():
                errors.append("Report is empty")
            claims, claims_loaded = final_json("claims.json")
            verification, verification_loaded = final_json("verification.json")
            coverage, _ = final_json("coverage.json")
            if claims_loaded:
                if not isinstance(claims, list) or not claims:
                    errors.append("claims.json must be a non-empty array")
                else:
                    claim_ids = set()
                    for claim in claims:
                        if not isinstance(claim, dict):
                            errors.append("Malformed claim")
                            continue
                        identifier = claim.get("id")
                        category = claim.get("category")
                        sources = claim.get("evidence_ids", [])
                        metric_ids = claim.get("metric_ids", [])
                        if not isinstance(identifier, str) or not identifier or identifier in claim_ids or not claim.get("text"):
                            errors.append("Missing or duplicate claim ID/text")
                        if isinstance(identifier, str):
                            claim_ids.add(identifier)
                        if category not in {"fact", "inference", "scenario"}:
                            errors.append("Unknown claim category: " + str(identifier))
                        if (not isinstance(sources, list) or not isinstance(metric_ids, list)
                                or any(not isinstance(value, str) for value in sources + metric_ids)):
                            errors.append("Claim references must be arrays of IDs: " + str(identifier))
                            continue
                        if category == "fact" and not sources and not metric_ids:
                            errors.append("Uncited fact: " + str(identifier))
                        if any(source not in evidence or evidence[source]["status"] != "ok" for source in sources):
                            errors.append("Claim cites unknown/failed evidence: " + str(identifier))
                        if any(metric_id not in by_id for metric_id in metric_ids):
                            errors.append("Claim cites unknown metric: " + str(identifier))
                        support = sources + [source for metric_id in metric_ids if metric_id in by_id
                                             for source in by_id[metric_id]["evidence_ids"]]
                        if category == "fact" and support and all(evidence.get(source, {}).get("kind") == "search" for source in support):
                            errors.append("Search discovery cannot substantiate a fact without original sources: " + str(identifier))
            if verification_loaded:
                state =verification.get("status") if isinstance(verification, dict) else None
                if state not in {"independent", "same_model_or_unknown", "not_performed"}:
                    errors.append("Invalid verification status")
                elif state == "independent":
                    if not verification.get("writer_model") or not verification.get("reviewer_model") or verification["writer_model"] == verification["reviewer_model"]:
                        errors.append("Independent verification requires distinct recorded models")
                    if not (directory / "review.md").is_file():
                        errors.append("Independent verification requires a saved review")
                    warnings.append("Model-family independence and review quality require manual verification")
                else:
                    warnings.append("No confirmed independent cross-model verification")
                    notices = verification.get("warnings")
                    if not isinstance(notices, list) or not notices or any(not isinstance(notice, str) or not notice.strip() for notice in notices):
                        errors.append("Missing explicit warning about limited verification")
                    if (directory / "report.md").is_file() and VERIFICATION_NOTICE not in report_text:
                        errors.append("Report must include the exact notice: " + VERIFICATION_NOTICE)
            from .quality import assess
            # A malformed coverage.json is already an error; don't also report it missing.
            required = manifest.get("coverage_schema") == 1 and not (directory / "coverage.json").is_file()
            quality, quality_errors = assess(coverage, evidence, claims, required=required, verification=verification)
            errors.extend(quality_errors)
            if quality["status"] != "adequate":
                warnings.append("Research coverage is " + quality["status"] + "; artifact validity is not answer completeness")
            if _snapshot and not errors:
                snapshot = {"manifest": manifest, "evidence": records, "metrics": metrics,
                            "claims": claims, "verification": verification, "report": report_text,
                            "coverage": coverage, "quality": quality,
                            "report_sha256": hashlib.sha256(report_bytes).hexdigest()}
        warnings.append("Structural checks do not prove source truth, claim entailment, dimensional correctness or investment quality")
    result = {"ok": not errors, "errors": errors, "warnings": warnings, "checked_at": stamp()}
    if final:
        result["quality"] = quality
    if snapshot is not None:
        result["_snapshot"] = snapshot
    return result


def publish(run, *, project_root=None):
    from .publication import publish_report
    return publish_report(run, project_root=project_root)
