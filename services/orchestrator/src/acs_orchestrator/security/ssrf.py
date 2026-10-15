"""Outbound URL validation (SSRF protection).

The Agent-Readiness Auditor drives a headless browser against storefront URLs.
A URL that came from a chat message must never point it at internal addresses or
at sites the merchant does not own, so we validate before delegating:

1. https only, default port, no credentials in the URL;
2. host must be one of the merchant's registered storefront domains (or a subdomain);
3. every address the host resolves to must be globally routable.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    pass


def _host_allowed(host: str, allowed_domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for domain in allowed_domains:
        domain = domain.lower().strip().rstrip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return True
    return False


def parse_and_check(url: str, allowed_domains: list[str]) -> tuple[str, str]:
    """Static checks. Returns (normalized_url, host)."""
    if len(url) > 2048:
        raise UnsafeUrlError("URL too long")
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise UnsafeUrlError("only https URLs can be audited")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials in URL are not allowed")
    if parts.port not in (None, 443):
        raise UnsafeUrlError("non-default ports are not allowed")
    host = parts.hostname or ""
    if not host:
        raise UnsafeUrlError("URL has no host")
    try:
        ipaddress.ip_address(host)
        raise UnsafeUrlError("IP-literal hosts are not allowed")
    except ValueError as exc:
        if isinstance(exc, UnsafeUrlError):
            raise
    if not _host_allowed(host, allowed_domains):
        raise UnsafeUrlError("host is not a registered storefront domain for this merchant")
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{path}{query}", host


async def assert_public_host(host: str) -> None:
    """DNS check: every resolved address must be globally routable."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError("host does not resolve") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global or addr.is_multicast:
            raise UnsafeUrlError("host resolves to a non-public address")


async def validate_storefront_url(
    url: str, allowed_domains: list[str], *, resolve: bool = True
) -> str:
    normalized, host = parse_and_check(url, allowed_domains)
    if resolve:
        await assert_public_host(host)
    return normalized
