from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "sessionid", "sid", "ref", "source",
    "sort", "order", "view", "limit", "campaign", "cmpid",
}
DENIED_PARAM_PREFIXES = ("utm_",)
FACET_PARAM_PREFIXES = ("filter", "facet", "prefn", "prefv")
DEFAULT_DENIED_PATHS = re.compile(
    r"/(account|login|register|checkout|basket|cart|customer-service|stores?"
    r"|store-locator|careers?|corporate|legal|privacy|terms|search)(/|$)",
    re.IGNORECASE,
)


class UnsafeUrl(ValueError):
    pass


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def validate_public_url(url: str, *, expected_domain: str | None = None, allow_subdomains: bool = False) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrl("Only absolute HTTP and HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeUrl("Credentials in URLs are not allowed")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeUrl("Local hosts are blocked")
    if expected_domain:
        domain = expected_domain.lower().rstrip(".")
        valid = host == domain or (allow_subdomains and host.endswith("." + domain))
        if not valid:
            raise UnsafeUrl("URL is outside the approved domain")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or default_port, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise UnsafeUrl("Hostname could not be resolved") from exc
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeUrl("Hostname resolves to a private or reserved address")
    return host


def normalize_url(url: str, base_url: str | None = None) -> str:
    absolute = urljoin(base_url, url) if base_url else url
    parsed = urlsplit(absolute)
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    netloc = host if port is None or (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    params = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        lower = key.lower()
        if lower in TRACKING_PARAMS or any(lower.startswith(prefix) for prefix in DENIED_PARAM_PREFIXES + FACET_PARAM_PREFIXES):
            continue
        params.append((key, value))
    return urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(sorted(params)), ""))


def path_is_irrelevant(url: str) -> bool:
    return bool(DEFAULT_DENIED_PATHS.search(urlsplit(url).path))
