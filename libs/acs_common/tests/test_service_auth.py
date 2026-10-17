from __future__ import annotations

import base64
import json
import time

import pytest

from acs_common.service_auth import STRIPPED_SIGNATURE, CallerAllowList, CallerRejected


def _seg(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def stripped_token(**claims) -> str:
    payload = {"email": "a@p.iam.gserviceaccount.com", "email_verified": True}
    payload["exp"] = time.time() + 600
    payload.update(claims)
    return f"Bearer {_seg({'alg': 'RS256'})}.{_seg(payload)}.{STRIPPED_SIGNATURE}"


async def test_disabled_allows_everything():
    assert await CallerAllowList([], enabled=False).caller(None) == "local"


async def test_stripped_token_trusted_only_on_cloud_run(monkeypatch):
    allow = CallerAllowList(["a@p.iam.gserviceaccount.com"], enabled=True)
    monkeypatch.delenv("K_SERVICE", raising=False)
    with pytest.raises(CallerRejected):
        await allow.caller(stripped_token())
    monkeypatch.setenv("K_SERVICE", "acs-rule-engine")
    assert await allow.caller(stripped_token()) == "a@p.iam.gserviceaccount.com"


async def test_caller_not_on_list_rejected(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "svc")
    allow = CallerAllowList(["b@p.iam.gserviceaccount.com"], enabled=True)
    with pytest.raises(CallerRejected):
        await allow.caller(stripped_token())
    with pytest.raises(CallerRejected):
        await CallerAllowList(["a@p.iam.gserviceaccount.com"], enabled=True).caller(
            stripped_token(email_verified=False)
        )


async def test_expired_and_malformed_rejected(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "svc")
    allow = CallerAllowList(["a@p.iam.gserviceaccount.com"], enabled=True)
    with pytest.raises(CallerRejected):
        await allow.caller(stripped_token(exp=time.time() - 3600))
    for header in (None, "Basic abc", "Bearer a.b", f"Bearer !!.??.{STRIPPED_SIGNATURE}"):
        with pytest.raises(CallerRejected):
            await allow.caller(header)


async def test_signed_token_is_verified(monkeypatch):
    allow = CallerAllowList(["a@p.iam.gserviceaccount.com"], enabled=True)
    monkeypatch.setattr(
        allow,
        "_verify_signed",
        lambda token: {"email": "a@p.iam.gserviceaccount.com", "email_verified": True},
    )
    assert await allow.caller("Bearer h.p.realsig") == "a@p.iam.gserviceaccount.com"

    def bad(token):
        raise ValueError("bad signature")

    monkeypatch.setattr(allow, "_verify_signed", bad)
    with pytest.raises(CallerRejected):
        await allow.caller("Bearer h.p.realsig")
