from __future__ import annotations

import base64
import json

from conftest import ANALYST, OTHER_TENANT, PARTNER, PDF_BYTES, VIEWER, bearer

from acs_orchestrator.clients.gmail import MailCandidate

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32


def _upload(client, data, headers, name="po.pdf"):
    return client.post(
        "/v1/intake/uploads",
        files={"file": (name, data, "application/pdf")},
        data={"customer_ref": "ACME-1"},
        headers=headers,
    )


def test_upload_publishes_po_event(harness):
    r = _upload(harness.client, PDF_BYTES, ANALYST, name="../../etc/passwd.pdf")
    assert r.status_code == 202, r.text
    receipt = r.json()
    assert receipt["status"] == "queued"
    data, attrs = harness.publisher.messages[0]
    assert data == PDF_BYTES
    assert attrs["merchant_id"] == "m1"
    assert attrs["content_type"] == "application/pdf"
    assert attrs["filename"] == "passwd.pdf"
    assert attrs["customer_ref"] == "ACME-1"
    assert attrs["intake_id"] == receipt["intake_id"]

    again = _upload(harness.client, PDF_BYTES, ANALYST).json()
    assert again["status"] == "duplicate"
    assert len(harness.publisher.messages) == 1

    status = harness.client.get(f"/v1/intake/{receipt['intake_id']}", headers=VIEWER).json()
    assert status["status"] == "queued"
    # Another tenant cannot see it.
    r = harness.client.get(f"/v1/intake/{receipt['intake_id']}", headers=OTHER_TENANT)
    assert r.status_code == 404


def test_audio_upload_sniffed_by_magic_bytes(harness):
    r = _upload(harness.client, WAV, ANALYST, name="voice.pdf")
    assert r.status_code == 202
    assert harness.publisher.messages[0][1]["content_type"] == "audio/wav"


def test_rejects_unsupported_and_active_content(harness):
    assert _upload(harness.client, b"MZ\x90\x00binary", ANALYST).status_code == 415
    evil = b"%PDF-1.7\n<< /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >>"
    assert _upload(harness.client, evil, ANALYST).status_code == 415
    assert harness.publisher.messages == []


def test_viewer_cannot_upload(harness):
    assert _upload(harness.client, PDF_BYTES, VIEWER).status_code == 403


def test_partner_webhook(harness):
    body = {"filename": "po-77.pdf", "content_base64": base64.b64encode(PDF_BYTES).decode()}
    r = harness.client.post("/v1/intake/webhook", json=body, headers=PARTNER)
    assert r.status_code == 202, r.text
    assert harness.publisher.messages[0][1]["customer_ref"] == "partner1"
    bad = {"filename": "x.pdf", "content_base64": "%%%not-base64"}
    assert harness.client.post("/v1/intake/webhook", json=bad, headers=PARTNER).status_code == 415
    # Merchant users must use the upload endpoint.
    assert harness.client.post("/v1/intake/webhook", json=body, headers=ANALYST).status_code == 401


def _push(history_id: int, mailbox: str = "orders@m1.test") -> dict:
    data = json.dumps({"emailAddress": mailbox, "historyId": history_id}).encode()
    return {
        "message": {"data": base64.b64encode(data).decode(), "messageId": "1"},
        "subscription": "projects/p/subscriptions/gmail",
    }


class FakeGmail:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_since(self, start: str):
        self.calls.append(start)
        return [
            MailCandidate("m-1", "buyer.example", "po.pdf", PDF_BYTES, is_body=False),
            MailCandidate(
                "m-1", "buyer.example", "m-1.txt", b"Please ship 10 x SKU-1", is_body=True
            ),
            MailCandidate("m-1", "buyer.example", "malware.exe", b"MZ\x90", is_body=False),
        ], "200"


def test_gmail_push_flow(harness):
    c = harness.client
    assert c.post("/v1/intake/gmail-push", json=_push(100)).status_code == 401
    assert (
        c.post("/v1/intake/gmail-push", json=_push(100), headers=bearer("forged")).status_code
        == 401
    )

    harness.ops.gmail = FakeGmail()
    push = bearer("push-token")
    # First notification only stores the cursor.
    assert c.post("/v1/intake/gmail-push", json=_push(100), headers=push).status_code == 204
    assert harness.publisher.messages == []
    # Next notification pulls history since the cursor; the .exe is dropped.
    assert c.post("/v1/intake/gmail-push", json=_push(150), headers=push).status_code == 204
    assert harness.ops.gmail.calls == ["100"]
    types = sorted(a["content_type"] for _, a in harness.publisher.messages)
    assert types == ["application/pdf", "text/plain"]
    assert all(a["source"] == "gmail" for _, a in harness.publisher.messages)
    # Notifications for other mailboxes are ignored.
    assert (
        c.post("/v1/intake/gmail-push", json=_push(300, "ceo@m1.test"), headers=push).status_code
        == 204
    )
    assert harness.ops.gmail.calls == ["100"]
