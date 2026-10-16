from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import (
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from google.protobuf import json_format, struct_pb2

from acs_auditor.advisor import FixAdvisor
from acs_auditor.browser import UnsafeUrlError, parse_json_ld, parse_sitemap, parse_store_url
from acs_auditor.checks import (
    CHECKS,
    JOURNEYS,
    PageSnapshot,
    SiteSnapshot,
    failed_checks,
    overall,
    robots_blocks_agents,
    score,
)
from acs_auditor.config import Settings
from acs_auditor.infra import InProcessDispatcher, MemorySink
from acs_auditor.main import create_app

PRODUCT_LD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Blue widget",
    "sku": "WID-BLUE",
    "offers": {
        "@type": "Offer",
        "price": "12.50",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
        "hasMerchantReturnPolicy": {"@type": "MerchantReturnPolicy", "merchantReturnDays": 30},
    },
}


def ready_site() -> SiteSnapshot:
    origin = "https://shop.example.com"
    return SiteSnapshot(
        store_url=f"{origin}/",
        host="shop.example.com",
        ucp_status=200,
        ucp_profile={
            "ucp": {
                "version": "2026-04-08",
                "services": {"dev.ucp.shopping": [{"transport": "rest"}]},
                "capabilities": {
                    "dev.ucp.shopping.checkout": {},
                    "com.acs.shopping.refund_decision": {},
                },
                "payment_handlers": {"com.example.pay": {}},
            }
        },
        robots_status=200,
        robots_txt="User-agent: *\nDisallow: /admin\n",
        sitemap_status=200,
        sitemap_urls=[f"{origin}/products/blue-widget"],
        home=PageSnapshot(
            url=f"{origin}/",
            status=200,
            title="Shop",
            meta_description="Widgets",
            json_ld=[{"@graph": [{"@type": "Organization", "name": "Shop"}]}],
            links=[(f"{origin}/policies/returns", "Returns")],
        ),
        product=PageSnapshot(
            url=f"{origin}/products/blue-widget",
            status=200,
            json_ld=[PRODUCT_LD],
            buttons=["Add to cart"],
            controls_total=2,
            controls_unlabeled=0,
        ),
        add_to_cart_clicked=True,
        cart=PageSnapshot(url=f"{origin}/cart", status=200, text_excerpt="Blue widget x1"),
        checkout=PageSnapshot(url=f"{origin}/checkout", status=200),
        returns=PageSnapshot(url=f"{origin}/policies/returns", status=200),
    )


# ------------------------------------------------------------------ deterministic checks
def test_agent_ready_site_scores_100():
    results = score(ready_site(), list(JOURNEYS))
    assert [r.score for r in results] == [100.0] * 5
    assert overall(results) == 100.0
    assert failed_checks(results) == []


def test_bare_site_scores_low_and_lists_fixes():
    site = SiteSnapshot(store_url="https://x.example/", host="x.example", robots_txt="")
    results = score(site, ["discovery", "checkout"])
    assert overall(results) < 25
    failed = {c.id for c in failed_checks(results)}
    assert {"ucp_profile_published", "ucp_checkout_capability"} <= failed


def test_captcha_and_blocked_robots_fail():
    site = ready_site()
    site.robots_txt = "User-agent: GPTBot\nDisallow: /\n"
    site.checkout.has_captcha = True
    failed = {c.id for c in failed_checks(score(site, ["discovery", "checkout"]))}
    assert {"robots_allows_agents", "no_captcha_wall"} <= failed


@pytest.mark.parametrize(
    ("robots", "blocked"),
    [
        ("User-agent: *\nDisallow: /\n", True),
        ("User-agent: *\nDisallow: /cart\n", False),
        ("User-agent: BadBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", False),
        ("User-agent: ClaudeBot\nUser-agent: Foo\nDisallow: /   # no\n", True),
        ("", False),
    ],
)
def test_robots_parsing(robots, blocked):
    assert robots_blocks_agents(robots) is blocked


def test_every_check_has_a_known_journey_and_fix():
    assert {c.journey for c in CHECKS} == set(JOURNEYS)
    assert all(c.fix and c.weight in (1, 2, 3) for c in CHECKS)
    assert len({c.id for c in CHECKS}) == len(CHECKS)


def test_broken_snapshot_data_does_not_crash_scoring():
    site = ready_site()
    site.product.json_ld = [{"@type": "Product", "offers": "not-an-object"}, [1, 2]]  # type: ignore[list-item]
    results = score(site, ["product_detail"])
    assert 0 <= results[0].score < 100


# ------------------------------------------------------------------ URL safety and parsing
@pytest.mark.parametrize(
    "url",
    [
        "http://shop.example.com/",
        "https://user:pw@shop.example.com/",
        "https://shop.example.com:8443/",
        "https://10.0.0.1/",
        "https://[::1]/",
        "https://localhost/",
        "file:///etc/passwd",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeUrlError):
        parse_store_url(url)


def test_url_normalized():
    assert parse_store_url("https://Shop.Example.com.") == (
        "https://shop.example.com/",
        "shop.example.com",
    )


def test_sitemap_parsing_rejects_dtds_and_foreign_hosts():
    body = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://shop.example.com/products/a</loc></url>"
        b"<url><loc>https://evil.example/products/b</loc></url></urlset>"
    )
    assert parse_sitemap(body, "shop.example.com") == ["https://shop.example.com/products/a"]
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset>&a;</urlset>'
    assert parse_sitemap(bomb, "shop.example.com") == []
    assert parse_sitemap(b"not xml", "shop.example.com") == []


def test_json_ld_parsing_skips_garbage():
    blocks = [json.dumps(PRODUCT_LD), "{not json", json.dumps([{"@type": "WebSite"}, 3])]
    assert [b["@type"] for b in parse_json_ld(blocks)] == ["Product", "WebSite"]


# ------------------------------------------------------------------ advisor
class FakeGemini:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, *, model, contents, config):
        self.prompts.append(contents)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(text=json.dumps(self.reply))


async def test_advisor_only_rewrites_failed_checks():
    site = SiteSnapshot(store_url="https://x.example/", host="x.example")
    failed = [c for c in CHECKS if c.id in ("ucp_profile_published", "sitemap_available")]
    gemini = FakeGemini(
        {
            "fixes": [
                {"check_id": "ucp_profile_published", "suggested_fix": "Add /.well-known/ucp"},
                {"check_id": "made_up", "suggested_fix": "ignore me"},
            ]
        }
    )
    fixes = await FixAdvisor(gemini, "m").suggest(site, failed)
    by_id = {f["check_id"]: f for f in fixes}
    assert set(by_id) == {"ucp_profile_published", "sitemap_available"}
    assert by_id["ucp_profile_published"]["suggested_fix"] == "Add /.well-known/ucp"
    assert by_id["ucp_profile_published"]["severity"] == "high"
    assert "<untrusted>" in gemini.prompts[0][0]


async def test_advisor_falls_back_when_model_fails():
    from google.genai import errors

    failed = [CHECKS[0]]
    gemini = FakeGemini(errors.APIError(429, {"error": {"message": "q", "status": "R"}}))
    fixes = await FixAdvisor(gemini, "m").suggest(SiteSnapshot("https://x.example/", "x"), failed)
    assert fixes[0]["suggested_fix"] == CHECKS[0].fix


# ------------------------------------------------------------------ A2A end to end
class FakeCollector:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot or ready_site()
        self.error = error
        self.calls = []

    async def collect(self, store_url, journeys):
        self.calls.append((store_url, journeys))
        if self.error:
            raise self.error
        return self.snapshot


async def a2a_client(http):
    card = await A2ACardResolver(http, "http://auditor").get_agent_card()
    for interface in card.supported_interfaces:
        interface.url = "http://auditor/"
    return ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)


async def send_audit(client, payload):
    data = struct_pb2.Value()
    json_format.ParseDict(payload, data)
    request = SendMessageRequest(
        message=Message(
            message_id=uuid.uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(data=data, media_type="application/json")],
        ),
        configuration=SendMessageConfiguration(return_immediately=True),
    )
    last = None
    async for response in client.send_message(request):
        last = response
    return last.task


AUDIT = {
    "skill": "audit_store",
    "merchant_id": "m1",
    "store_url": "https://shop.example.com/",
    "journeys": ["discovery", "product_detail", "cart", "checkout"],
    "requested_by": "u-analyst",
}


@pytest.fixture
async def auditor():
    collector = FakeCollector()
    sink = MemorySink()
    app = create_app(
        Settings(env="local", self_url="http://auditor"),
        collector=collector,
        advisor=FixAdvisor(None, "m"),
        sink=sink,
        dispatcher=InProcessDispatcher(),
        resolve_dns=False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://auditor"
    ) as http:
        yield SimpleNamespace(
            app=app, http=http, client=await a2a_client(http), collector=collector, sink=sink
        )


async def wait_terminal(client, task_id):
    for _ in range(100):
        task = await client.get_task(GetTaskRequest(id=task_id))
        if task.status.state not in (
            TaskState.TASK_STATE_SUBMITTED,
            TaskState.TASK_STATE_WORKING,
        ):
            return task
        await asyncio.sleep(0.05)
    raise AssertionError("audit did not finish")


async def test_audit_returns_immediately_then_completes(auditor):
    task = await send_audit(auditor.client, AUDIT)
    assert task.status.state == TaskState.TASK_STATE_SUBMITTED
    done = await wait_terminal(auditor.client, task.id)
    assert done.status.state == TaskState.TASK_STATE_COMPLETED
    result = json_format.MessageToDict(done.artifacts[0].parts[0].data)
    assert result["audit_id"] == task.id
    assert result["score"] == 100.0
    assert [j["journey"] for j in result["journeys"]] == AUDIT["journeys"]
    assert auditor.collector.calls == [("https://shop.example.com/", AUDIT["journeys"])]
    assert len(auditor.sink.scores) == 4
    assert {r["merchant_id"] for r in auditor.sink.scores} == {"m1"}


async def test_failed_checks_become_fix_rows(auditor):
    auditor.collector.snapshot = SiteSnapshot(store_url="https://shop.example.com/",
                                              host="shop.example.com")  # fmt: skip
    task = await send_audit(auditor.client, AUDIT)
    done = await wait_terminal(auditor.client, task.id)
    result = json_format.MessageToDict(done.artifacts[0].parts[0].data)
    assert result["score"] < 20
    assert result["fixes"] and len(auditor.sink.fixes) == len(result["fixes"])
    assert {f["severity"] for f in auditor.sink.fixes} <= {"high", "medium", "low"}


@pytest.mark.parametrize(
    "override",
    [
        {"store_url": "http://shop.example.com/"},
        {"store_url": "https://192.168.1.10/"},
        {"journeys": ["teleport"]},
        {"skill": "rm_rf"},
        {"extra": True},
    ],
)
async def test_bad_requests_rejected_without_browsing(auditor, override):
    task = await send_audit(auditor.client, {**AUDIT, **override})
    assert task.status.state == TaskState.TASK_STATE_REJECTED
    await asyncio.sleep(0.1)
    assert auditor.collector.calls == []


async def test_unsafe_url_at_run_time_fails_task(auditor):
    auditor.collector.error = UnsafeUrlError("host resolves to a non-public address")
    task = await send_audit(auditor.client, AUDIT)
    done = await wait_terminal(auditor.client, task.id)
    assert done.status.state == TaskState.TASK_STATE_FAILED
    assert "non-public" in done.status.message.parts[0].text
    assert auditor.sink.scores == []


async def test_run_endpoint_is_idempotent_and_handles_unknown_tasks(auditor):
    task = await send_audit(auditor.client, AUDIT)
    await wait_terminal(auditor.client, task.id)
    again = await auditor.http.post(f"/internal/audits/{task.id}/run")
    assert again.json() == {"outcome": "skipped"}
    assert len(auditor.collector.calls) == 1
    auditor.app.state.runner._wait = 0
    missing = await auditor.http.post("/internal/audits/does-not-exist/run")
    assert missing.status_code == 503
    bad = await auditor.http.post("/internal/audits/..%2Fx/run")
    assert bad.status_code in (404, 503)


async def test_callers_enforced_outside_local(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    app = create_app(
        Settings(
            env="prod",
            self_url="http://auditor",
            allowed_callers=["acs-orchestrator@p.iam.gserviceaccount.com"],
            runner_callers=["acs-auditor@p.iam.gserviceaccount.com"],
        ),
        task_store=None,
        collector=FakeCollector(),
        advisor=FixAdvisor(None, "m"),
        sink=MemorySink(),
        dispatcher=InProcessDispatcher(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://auditor"
    ) as http:
        assert (await http.get("/health")).status_code == 200
        assert (await http.get("/.well-known/agent-card.json")).status_code == 403
        assert (await http.post("/", json={})).status_code == 403
        assert (await http.post("/internal/audits/x/run")).status_code == 403
