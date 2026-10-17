"""Gmail B2B inbox reader - diagram channel "B2B Inbox (Gmail API push ...)".

Flow: Gmail `users.watch` -> Pub/Sub topic -> authenticated push -> API Gateway ->
orchestrator `/v1/intake/gmail-push` -> this reader pulls new messages since the
last stored historyId and returns PO candidates (attachments + text body).

Security:
* OAuth scope is `gmail.readonly` only; credentials come from Secret Manager.
* Only senders on the allow-listed B2B domains are processed.
* The sender must pass DMARC (or both SPF and DKIM) per Gmail's
  Authentication-Results header, so spoofed From headers are dropped.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_MAX_MESSAGES_PER_PUSH = 25


@dataclass(frozen=True)
class MailCandidate:
    message_id: str
    sender_domain: str
    filename: str
    data: bytes
    is_body: bool


def sender_authenticated(headers: dict[str, str]) -> bool:
    results = headers.get("authentication-results", "").lower()
    if re.search(r"\bdmarc=pass\b", results):
        return True
    return bool(re.search(r"\bspf=pass\b", results) and re.search(r"\bdkim=pass\b", results))


def sender_domain(headers: dict[str, str]) -> str:
    _, address = parseaddr(headers.get("from", ""))
    return address.rpartition("@")[2].lower().strip()


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    return any(domain == d.lower() or domain.endswith("." + d.lower()) for d in allowed if d)


class GmailReader:
    def __init__(self, oauth_json: str, allowed_domains: list[str], max_bytes: int) -> None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        info = json.loads(oauth_json)
        creds = Credentials(
            token=None,
            refresh_token=info["refresh_token"],
            client_id=info["client_id"],
            client_secret=info["client_secret"],
            token_uri="https://oauth2.googleapis.com/token",  # noqa: S106 - public endpoint
            scopes=GMAIL_SCOPES,
        )
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._allowed = allowed_domains
        self._max_bytes = max_bytes

    async def fetch_since(self, start_history_id: str) -> tuple[list[MailCandidate], str]:
        return await asyncio.to_thread(self._fetch_since, start_history_id)

    def _fetch_since(self, start_history_id: str) -> tuple[list[MailCandidate], str]:
        users = self._service.users()
        message_ids: list[str] = []
        latest = start_history_id
        page_token = None
        while True:
            resp = (
                users.history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
            latest = resp.get("historyId", latest)
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    if "INBOX" in msg.get("labelIds", []) and msg.get("id"):
                        message_ids.append(msg["id"])
            page_token = resp.get("nextPageToken")
            if not page_token or len(message_ids) >= _MAX_MESSAGES_PER_PUSH:
                break

        candidates: list[MailCandidate] = []
        for message_id in dict.fromkeys(message_ids[:_MAX_MESSAGES_PER_PUSH]):
            candidates.extend(self._candidates_for(users, message_id))
        return candidates, str(latest)

    def _candidates_for(self, users: Any, message_id: str) -> list[MailCandidate]:
        msg = users.messages().get(userId="me", id=message_id, format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        domain = sender_domain(headers)
        if not domain_allowed(domain, self._allowed):
            logger.info("gmail: skipping message from non-allow-listed domain")
            return []
        if not sender_authenticated(headers):
            logger.warning("gmail: dropping message that failed SPF/DKIM/DMARC")
            return []

        out: list[MailCandidate] = []
        body_text: list[str] = []
        stack = [payload]
        while stack:
            part = stack.pop()
            stack.extend(part.get("parts", []))
            body = part.get("body", {})
            filename = part.get("filename") or ""
            if filename and body.get("attachmentId"):
                if body.get("size", 0) > self._max_bytes:
                    logger.warning("gmail: attachment over size limit skipped")
                    continue
                att = (
                    users.messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=body["attachmentId"])
                    .execute()
                )
                data = base64.urlsafe_b64decode(att["data"])
                out.append(MailCandidate(message_id, domain, filename, data, is_body=False))
            elif part.get("mimeType") == "text/plain" and body.get("data") and not filename:
                body_text.append(base64.urlsafe_b64decode(body["data"]).decode("utf-8", "replace"))
        text = "\n".join(t for t in body_text if t.strip())
        if text.strip():
            subject = headers.get("subject", "")[:200]
            raw = f"Subject: {subject}\n\n{text}".encode()[: self._max_bytes]
            data = raw.decode("utf-8", "ignore").encode()  # don't split a multi-byte char
            out.append(MailCandidate(message_id, domain, f"{message_id}.txt", data, is_body=True))
        return out
