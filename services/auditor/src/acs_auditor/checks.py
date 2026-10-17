"""Deterministic agent-readiness checks and scoring.

Scores come only from these checks over what the browser observed; Gemini never
decides a score. Each journey scores 0-100 by passed check weight, and the audit
score is the mean over the requested journeys.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from typing import Any

JOURNEYS = ("discovery", "product_detail", "cart", "checkout", "returns")

# User agents AI shopping assistants use; a site-wide Disallow for any of them (or "*")
# blocks agents from the catalog.
AGENT_USER_AGENTS = ("*", "gptbot", "oai-searchbot", "chatgpt-user", "claudebot",
                     "claude-user", "perplexitybot", "google-extended", "googlebot")  # fmt: skip
_ADD_TO_CART = re.compile(r"\badd\s+to\s+(cart|bag|basket)\b|\bbuy\s+now\b", re.I)
_EMPTY_CART = re.compile(r"(cart|bag|basket)\s+is\s+(currently\s+)?empty|no\s+items", re.I)


@dataclass
class PageSnapshot:
    url: str
    status: int | None = None
    title: str = ""
    meta_description: str = ""
    lang: str = ""
    json_ld: list[dict[str, Any]] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, text)
    buttons: list[str] = field(default_factory=list)  # accessible names
    controls_total: int = 0
    controls_unlabeled: int = 0
    has_captcha: bool = False
    text_excerpt: str = ""


@dataclass
class SiteSnapshot:
    store_url: str
    host: str
    ucp_status: int | None = None
    ucp_profile: dict[str, Any] | None = None
    robots_status: int | None = None
    robots_txt: str = ""
    sitemap_status: int | None = None
    sitemap_urls: list[str] = field(default_factory=list)
    home: PageSnapshot | None = None
    product: PageSnapshot | None = None
    add_to_cart_clicked: bool = False
    cart: PageSnapshot | None = None
    checkout: PageSnapshot | None = None
    returns: PageSnapshot | None = None
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
def json_ld_nodes(blocks: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Every object in the JSON-LD blocks (flattens @graph, lists and nesting)."""
    stack: list[tuple[Any, int]] = [(b, 0) for b in blocks]
    while stack:
        node, depth = stack.pop()
        if depth > 8:
            continue
        if isinstance(node, list):
            stack.extend((n, depth + 1) for n in node[:200])
        elif isinstance(node, dict):
            yield node
            stack.extend((v, depth + 1) for v in node.values() if isinstance(v, dict | list))


def node_types(node: dict[str, Any]) -> set[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return {str(v).rsplit("/", 1)[-1].lower() for v in values if v}


def nodes_of(page: PageSnapshot | None, *types: str) -> list[dict[str, Any]]:
    if page is None:
        return []
    wanted = {t.lower() for t in types}
    return [n for n in json_ld_nodes(page.json_ld) if node_types(n) & wanted]


def ucp_capabilities(snapshot: SiteSnapshot) -> list[str]:
    ucp = (snapshot.ucp_profile or {}).get("ucp")
    caps = ucp.get("capabilities") if isinstance(ucp, dict) else None
    if isinstance(caps, dict):
        return [str(k).lower() for k in caps]
    if isinstance(caps, list):
        return [str(c.get("name", "")).lower() for c in caps if isinstance(c, dict)]
    return []


def robots_blocks_agents(robots: str) -> bool:
    """True when a group for '*' or a known agent UA disallows the whole site."""
    agents: list[str] = []
    in_rules = False
    for raw in robots.splitlines()[:2000]:
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
        elif key in ("disallow", "allow"):
            in_rules = True
            if key == "disallow" and value == "/" and any(a in AGENT_USER_AGENTS for a in agents):
                return True
    return False


# --------------------------------------------------------------------------- checks
@dataclass(frozen=True)
class Check:
    id: str
    journey: str
    weight: int
    title: str
    fix: str
    test: Callable[[SiteSnapshot], bool]

    @property
    def severity(self) -> str:
        return {3: "high", 2: "medium"}.get(self.weight, "low")


def _offer_complete(s: SiteSnapshot) -> bool:
    for product in nodes_of(s.product, "Product"):
        offers = product.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if isinstance(offer, dict) and all(
                offer.get(k) not in (None, "") for k in ("price", "priceCurrency", "availability")
            ):
                return True
            if isinstance(offer, dict) and offer.get("lowPrice") and offer.get("priceCurrency"):
                return True
    return False


def _controls_labeled(s: SiteSnapshot) -> bool:
    page = s.product or s.home
    if page is None or page.controls_total == 0:
        return page is not None
    return page.controls_unlabeled / page.controls_total <= 0.1


def _has_return_policy(s: SiteSnapshot) -> bool:
    pages = (s.home, s.product, s.returns)
    return any(nodes_of(p, "MerchantReturnPolicy") for p in pages) or any(
        "hasMerchantReturnPolicy" in n for p in pages if p for n in json_ld_nodes(p.json_ld)
    )


def _ok(page: PageSnapshot | None) -> bool:
    return page is not None and page.status is not None and page.status < 400


CHECKS: tuple[Check, ...] = (
    # discovery
    Check("ucp_profile_published", "discovery", 3, "UCP business profile at /.well-known/ucp",
          "Publish a UCP business profile at https://<store>/.well-known/ucp with ucp.version, "
          "services and capabilities so shopping agents can discover how to transact.",
          lambda s: s.ucp_status == 200 and isinstance((s.ucp_profile or {}).get("ucp"), dict)
          and bool(s.ucp_profile["ucp"].get("version"))),
    Check("ucp_services_declared", "discovery", 2, "UCP profile declares services",
          "List the dev.ucp.shopping service with its transports (REST, MCP or A2A) and "
          "endpoints in the UCP profile.",
          lambda s: bool(((s.ucp_profile or {}).get("ucp") or {}).get("services"))),
    Check("robots_allows_agents", "discovery", 2, "robots.txt lets shopping agents in",
          "Remove site-wide 'Disallow: /' rules for '*' and AI shopping agents (GPTBot, "
          "ClaudeBot, PerplexityBot, Google-Extended...) or allow product and cart paths.",
          lambda s: not robots_blocks_agents(s.robots_txt)),
    Check("sitemap_available", "discovery", 1, "XML sitemap lists pages",
          "Serve /sitemap.xml (and reference it from robots.txt) listing product URLs.",
          lambda s: s.sitemap_status == 200 and bool(s.sitemap_urls)),
    Check("home_structured_data", "discovery", 2, "Home page describes the store in JSON-LD",
          "Add schema.org Organization/OnlineStore and WebSite JSON-LD to the home page.",
          lambda s: bool(nodes_of(s.home, "Organization", "OnlineStore", "Store", "WebSite"))),
    Check("home_meta", "discovery", 1, "Home page has a title and meta description",
          "Give the home page a descriptive <title> and <meta name=description>.",
          lambda s: bool(s.home and s.home.title and s.home.meta_description)),
    # product detail
    Check("product_page_found", "product_detail", 2, "Product pages are linked and reachable",
          "Link product pages from the home page or sitemap with crawlable <a href> links.",
          lambda s: _ok(s.product)),
    Check("product_json_ld", "product_detail", 3, "Product page has schema.org Product JSON-LD",
          "Embed schema.org Product JSON-LD (name, description, image, sku, offers) in "
          "server-rendered HTML on every product page.",
          lambda s: bool(nodes_of(s.product, "Product", "ProductGroup"))),
    Check("offer_complete", "product_detail", 3, "Offer has price, currency and availability",
          "Include offers.price, offers.priceCurrency and offers.availability in Product JSON-LD.",
          _offer_complete),
    Check("product_identifiers", "product_detail", 1, "Product carries sku/gtin/mpn",
          "Add sku and a GTIN or MPN to Product JSON-LD so agents can match items exactly.",
          lambda s: any(any(k in n for k in ("sku", "gtin", "gtin13", "gtin12", "gtin14",
                                               "gtin8", "mpn"))
                        for n in nodes_of(s.product, "Product", "ProductGroup"))),
    Check("accessible_controls", "product_detail", 1, "Form controls have accessible names",
          "Give every input, select and button an accessible name (label, aria-label).",
          _controls_labeled),
    # cart
    Check("add_to_cart_affordance", "cart", 3, "Add-to-cart button is identifiable",
          "Use a real <button> whose accessible name says 'Add to cart' on product pages.",
          lambda s: bool(s.product and any(_ADD_TO_CART.search(b) for b in s.product.buttons))),
    Check("add_to_cart_works", "cart", 2, "Adding to cart puts the item in the cart",
          "Make add-to-cart work without third-party scripts or pop-ups an agent cannot "
          "dismiss, and show the item on /cart.",
          lambda s: s.add_to_cart_clicked and _ok(s.cart)
          and not _EMPTY_CART.search(s.cart.text_excerpt if s.cart else "")),
    Check("ucp_cart_capability", "cart", 2, "UCP profile offers a cart or checkout capability",
          "Declare the dev.ucp.shopping.checkout capability so agents can build carts via API.",
          lambda s: any("checkout" in c or "cart" in c for c in ucp_capabilities(s))),
    # checkout
    Check("ucp_checkout_capability", "checkout", 3, "UCP checkout capability declared",
          "Implement and declare dev.ucp.shopping.checkout (with its schema) in the UCP profile.",
          lambda s: any("checkout" in c for c in ucp_capabilities(s))),
    Check("checkout_reachable", "checkout", 2, "Checkout page is reachable",
          "Serve /checkout (or the checkout path your cart links to) without errors.",
          lambda s: _ok(s.checkout)),
    Check("no_captcha_wall", "checkout", 2, "No CAPTCHA wall in cart or checkout",
          "Replace CAPTCHAs in the purchase path with risk-based checks and verified agent "
          "identity (UCP-Agent profiles, signed requests).",
          lambda s: not any(p.has_captcha for p in (s.cart, s.checkout) if p)),
    Check("payment_handlers_declared", "checkout", 1, "UCP payment handlers declared",
          "Declare supported payment handlers (for example agent payment tokens) in the UCP "
          "profile.",
          lambda s: bool(((s.ucp_profile or {}).get("ucp") or {}).get("payment_handlers"))),
    # returns
    Check("returns_page_linked", "returns", 2, "Returns policy page is linked",
          "Link a returns/refund policy page from the footer of every page.",
          lambda s: _ok(s.returns)),
    Check("return_policy_structured", "returns", 3, "Return policy is machine-readable",
          "Publish schema.org MerchantReturnPolicy (window, fees, method) in JSON-LD, "
          "referenced from Organization or Offer via hasMerchantReturnPolicy.",
          _has_return_policy),
    Check("ucp_returns_capability", "returns", 1, "UCP profile exposes refunds or returns",
          "Expose a refund/return capability in the UCP profile so agents can resolve issues.",
          lambda s: any("refund" in c or "return" in c for c in ucp_capabilities(s))),
)  # fmt: skip


@dataclass
class CheckResult:
    id: str
    title: str
    passed: bool
    weight: int
    severity: str


@dataclass
class JourneyResult:
    journey: str
    score: float
    checks: list[CheckResult]


def score(snapshot: SiteSnapshot, journeys: list[str]) -> list[JourneyResult]:
    results: list[JourneyResult] = []
    for journey in journeys:
        checks = [c for c in CHECKS if c.journey == journey]
        outcomes: list[CheckResult] = []
        for check in checks:
            try:
                passed = bool(check.test(snapshot))
            except Exception:  # a malformed page must not crash the audit
                passed = False
            outcomes.append(CheckResult(check.id, check.title, passed, check.weight,
                                        check.severity))  # fmt: skip
        total = sum(c.weight for c in outcomes) or 1
        earned = sum(c.weight for c in outcomes if c.passed)
        results.append(JourneyResult(journey, round(100.0 * earned / total, 1), outcomes))
    return results


def overall(results: list[JourneyResult]) -> float:
    return round(sum(r.score for r in results) / len(results), 1) if results else 0.0


def failed_checks(results: list[JourneyResult]) -> list[Check]:
    failed = {c.id for r in results for c in r.checks if not c.passed}
    return [c for c in CHECKS if c.id in failed]


def as_dicts(results: list[JourneyResult]) -> list[dict[str, Any]]:
    return [asdict(r) for r in results]
