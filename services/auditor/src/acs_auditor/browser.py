"""Playwright journeys against a storefront - diagram edge "Storefront: Simulate Journeys".

Network safety (the URL was checked by the orchestrator against the merchant's
registered domains; this is the second, DNS-rebinding-proof check):
  * https only, default port, no credentials, no IP literals;
  * the host is resolved once here, every address must be globally routable, and
    Chromium is pinned to that address (`--host-resolver-rules`), so a later DNS
    answer pointing at an internal address is never used;
  * every request is routed through a guard that aborts non-https URLs and any
    host other than the storefront itself (third-party scripts, internal hosts);
  * images, media and fonts are skipped; nothing is downloaded; no form is ever
    filled in or submitted. Adding one item to the cart is the only state change.

Page content is untrusted: it is reduced to a few structured fields here, and
`advisor.py` passes only those (clearly delimited) to Gemini.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import xml.etree.ElementTree as ET  # noqa: S405 - parsed with size limits, no entities
from typing import Any
from urllib.parse import urljoin, urlsplit

from .checks import _ADD_TO_CART, PageSnapshot, SiteSnapshot

logger = logging.getLogger(__name__)

USER_AGENT = "ACS-ReadinessAuditor/0.1 (+https://ucp.dev; agent-readiness check)"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_JSON_LD_BLOCKS = 20
_PRODUCT_PATH = re.compile(r"/(products?|p|item|items|dp|shop)/[^/?#]+", re.I)
_RETURNS = re.compile(r"return|refund", re.I)
_SKIP_TYPES = frozenset({"image", "media", "font"})


class UnsafeUrlError(ValueError):
    pass


def parse_store_url(url: str) -> tuple[str, str]:
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
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        raise UnsafeUrlError("URL has no public host name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeUrlError("IP-literal hosts are not allowed")
    return f"https://{host}{parts.path or '/'}", host


async def resolve_public(host: str) -> str:
    """Resolve once; every address must be public. Returns the address to pin."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError("host does not resolve") from exc
    addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or any(not a.is_global or a.is_multicast for a in addresses):
        raise UnsafeUrlError("host resolves to a non-public address")
    ipv4 = [a for a in addresses if a.version == 4]
    return str((ipv4 or addresses)[0])


_DOM_SCRIPT = """
() => {
  const text = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
  const name = (el) => (el.getAttribute('aria-label') || text(el) || el.value ||
                        el.getAttribute('title') || '').trim().slice(0, 120);
  const controls = Array.from(document.querySelectorAll(
    'input:not([type=hidden]), select, textarea'));
  const labeled = (el) => !!(el.getAttribute('aria-label') ||
    el.getAttribute('aria-labelledby') || (el.labels && el.labels.length) ||
    el.getAttribute('title'));
  const captcha = !!document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], ' +
    '[class*="captcha" i], [id*="captcha" i], .g-recaptcha, .h-captcha, .cf-turnstile');
  return {
    title: (document.title || '').slice(0, 300),
    meta_description: ((document.querySelector('meta[name="description"]') || {}).content
                       || '').slice(0, 500),
    lang: (document.documentElement.lang || '').slice(0, 20),
    json_ld: Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
               .slice(0, 20).map((s) => (s.textContent || '').slice(0, 200000)),
    links: Array.from(document.querySelectorAll('a[href]')).slice(0, 400)
             .map((a) => [a.href, text(a).slice(0, 80)]),
    buttons: Array.from(document.querySelectorAll(
               'button, input[type=submit], input[type=button], [role=button]'))
               .slice(0, 200).map(name).filter(Boolean),
    controls_total: controls.length,
    controls_unlabeled: controls.filter((c) => !labeled(c)).length,
    has_captcha: captcha,
    text_excerpt: text(document.body).slice(0, 2000),
  };
}
"""


def parse_json_ld(raw_blocks: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in raw_blocks[:MAX_JSON_LD_BLOCKS]:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                out.append(item)
    return out


def parse_sitemap(body: bytes, host: str) -> list[str]:
    if b"<!DOCTYPE" in body[:2048] or b"<!ENTITY" in body:
        return []  # refuse DTDs outright (entity expansion)
    try:
        root = ET.fromstring(body[:MAX_BODY_BYTES])  # noqa: S314 - DTDs rejected above
    except ET.ParseError:
        return []
    urls = [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]
    return [u for u in urls if urlsplit(u).hostname == host][:500]


class StorefrontBrowser:
    def __init__(self, *, page_timeout_s: float, budget_s: float) -> None:
        self._timeout_ms = int(page_timeout_s * 1000)
        self._budget_s = budget_s

    async def collect(self, store_url: str, journeys: list[str]) -> SiteSnapshot:
        url, host = parse_store_url(store_url)
        address = await resolve_public(host)
        snapshot = SiteSnapshot(store_url=url, host=host)
        try:
            await asyncio.wait_for(
                self._run(snapshot, host, address, set(journeys)), timeout=self._budget_s
            )
        except TimeoutError:
            snapshot.errors.append("audit time budget exhausted")
        return snapshot

    async def _run(self, s: SiteSnapshot, host: str, address: str, journeys: set[str]) -> None:
        from playwright.async_api import async_playwright

        pinned = f"[{address}]" if ":" in address else address
        origin = f"https://{host}"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                args=[f"--host-resolver-rules=MAP {host} {pinned}", "--disable-dev-shm-usage"]
            )
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT,
                    service_workers="block",
                    accept_downloads=False,
                    ignore_https_errors=False,
                )
                await context.route("**/*", lambda route: self._guard(route, host))
                page = await context.new_page()

                s.ucp_status, body = await self._fetch(page, f"{origin}/.well-known/ucp", s)
                try:
                    profile = json.loads(body) if body else None
                    s.ucp_profile = profile if isinstance(profile, dict) else None
                except ValueError:
                    s.errors.append("UCP profile is not valid JSON")
                s.robots_status, body = await self._fetch(page, f"{origin}/robots.txt", s)
                s.robots_txt = body.decode("utf-8", "replace")[:200_000] if body else ""
                s.sitemap_status, body = await self._fetch(page, f"{origin}/sitemap.xml", s)
                s.sitemap_urls = parse_sitemap(body, host) if body else []

                s.home = await self._snapshot(page, s.store_url, s)
                product_url = self._product_url(s, host)
                if product_url and journeys & {"product_detail", "cart", "checkout"}:
                    s.product = await self._snapshot(page, product_url, s)
                if s.product and journeys & {"cart", "checkout"}:
                    s.add_to_cart_clicked = await self._add_to_cart(page)
                    s.cart = await self._snapshot(page, f"{origin}/cart", s)
                if "checkout" in journeys:
                    s.checkout = await self._snapshot(page, f"{origin}/checkout", s)
                returns_url = self._returns_url(s, host)
                if "returns" in journeys and returns_url:
                    s.returns = await self._snapshot(page, returns_url, s)
            finally:
                await browser.close()

    @staticmethod
    async def _guard(route: Any, host: str) -> None:
        request = route.request
        target = urlsplit(request.url)
        if (
            target.scheme != "https"
            or (target.hostname or "").lower() != host
            or target.port not in (None, 443)
            or request.resource_type in _SKIP_TYPES
        ):
            await route.abort()
            return
        await route.continue_()

    async def _fetch(self, page: Any, url: str, s: SiteSnapshot) -> tuple[int | None, bytes]:
        try:
            response = await page.goto(url, wait_until="commit", timeout=self._timeout_ms)
            if response is None:
                return None, b""
            body = await response.body()
            return response.status, body[:MAX_BODY_BYTES] if response.ok else b""
        except Exception as exc:
            s.errors.append(f"{urlsplit(url).path}: {type(exc).__name__}")
            return None, b""

    async def _snapshot(self, page: Any, url: str, s: SiteSnapshot) -> PageSnapshot:
        snap = PageSnapshot(url=url)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
            snap.status = response.status if response else None
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:  # noqa: S110 - best effort; long-polling sites never go idle
                pass
            dom = await page.evaluate(_DOM_SCRIPT)
        except Exception as exc:
            s.errors.append(f"{urlsplit(url).path or '/'}: {type(exc).__name__}")
            return snap
        snap.title = dom.get("title", "")
        snap.meta_description = dom.get("meta_description", "")
        snap.lang = dom.get("lang", "")
        snap.json_ld = parse_json_ld(dom.get("json_ld", []))
        snap.links = [(str(h), str(t)) for h, t in dom.get("links", []) if isinstance(h, str)]
        snap.buttons = [str(b) for b in dom.get("buttons", [])]
        snap.controls_total = int(dom.get("controls_total", 0))
        snap.controls_unlabeled = int(dom.get("controls_unlabeled", 0))
        snap.has_captcha = bool(dom.get("has_captcha"))
        snap.text_excerpt = str(dom.get("text_excerpt", ""))
        return snap

    @staticmethod
    def _same_host(url: str, host: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == "https" and (parts.hostname or "").lower() == host

    def _product_url(self, s: SiteSnapshot, host: str) -> str | None:
        candidates = [h for h, _ in (s.home.links if s.home else [])] + s.sitemap_urls
        for url in candidates:
            if self._same_host(url, host) and _PRODUCT_PATH.search(urlsplit(url).path):
                return url.split("#", 1)[0]
        return None

    def _returns_url(self, s: SiteSnapshot, host: str) -> str | None:
        for href, text in s.home.links if s.home else []:
            if self._same_host(href, host) and (_RETURNS.search(text) or _RETURNS.search(href)):
                return href.split("#", 1)[0]
        return urljoin(s.store_url, "/policies/refund-policy") if s.home else None

    async def _add_to_cart(self, page: Any) -> bool:
        try:
            button = page.get_by_role("button", name=_ADD_TO_CART).first
            await button.click(timeout=5000)
            await page.wait_for_timeout(2000)
            return True
        except Exception:
            return False
