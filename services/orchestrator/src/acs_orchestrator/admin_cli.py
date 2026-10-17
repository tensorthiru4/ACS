"""Operator CLI for provisioning tenants and credentials.

    uv run acs-admin register-merchant --merchant m1 --domain shop.example.com
    uv run acs-admin set-claims --uid <firebase-uid> --merchant m1 --role admin
    uv run acs-admin create-client --merchant m1 --kind shopper_agent \
        --ucp-profile https://agent.example/.well-known/ucp
    uv run acs-admin disable-client --client-id <id>
    uv run acs-admin gmail-watch --topic projects/<p>/topics/gmail-b2b-inbox

Needs Application Default Credentials with Firestore / Firebase Admin rights, plus
ACS_PROJECT_ID. Secrets (pepper, Gmail OAuth) are read from Secret Manager.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from datetime import UTC, datetime

from .security.auth import CLIENT_TOKEN_PREFIX, hash_client_secret
from .security.principals import PrincipalKind, Role

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _secret(project: str, name: str) -> str:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project}/secrets/{name}/versions/latest"
    return client.access_secret_version(name=path).payload.data.decode()


def _db(project: str):  # type: ignore[no-untyped-def]
    from google.cloud import firestore

    return firestore.Client(project=project)


def register_merchant(args: argparse.Namespace) -> None:
    domains = sorted({d.lower().strip().rstrip(".") for d in args.domain})
    _db(args.project).collection("merchants").document(args.merchant).set(
        {"storefront_domains": domains, "updated_at": datetime.now(UTC)}, merge=True
    )
    print(f"merchant {args.merchant}: storefront_domains={domains}")


def set_claims(args: argparse.Namespace) -> None:
    import firebase_admin
    from firebase_admin import auth

    firebase_admin.initialize_app(options={"projectId": args.project})
    auth.set_custom_user_claims(args.uid, {"merchant_id": args.merchant, "role": args.role})
    # Force existing sessions to re-authenticate so the new role applies immediately.
    auth.revoke_refresh_tokens(args.uid)
    print(f"uid {args.uid}: merchant_id={args.merchant} role={args.role} (tokens revoked)")


def create_client(args: argparse.Namespace) -> None:
    if args.kind == PrincipalKind.SHOPPER_AGENT.value and not args.ucp_profile:
        sys.exit("shopper_agent clients must be bound to a --ucp-profile (https URI)")
    if args.ucp_profile and not args.ucp_profile.startswith("https://"):
        sys.exit("--ucp-profile must be an https URI")
    pepper = _secret(args.project, "api-key-pepper")
    client_id = f"{args.kind.split('_')[0]}-{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    _db(args.project).collection("api_clients").document(client_id).create(
        {
            "secret_hash": hash_client_secret(secret, pepper),
            "merchant_id": args.merchant,
            "kind": args.kind,
            "ucp_profile": args.ucp_profile,
            "display": args.display or client_id,
            "disabled": False,
            "created_at": datetime.now(UTC),
        }
    )
    print("Store this credential now; it cannot be shown again:")
    print(f"  Authorization: Bearer {CLIENT_TOKEN_PREFIX}{client_id}.{secret}")


def disable_client(args: argparse.Namespace) -> None:
    _db(args.project).collection("api_clients").document(args.client_id).update(
        {"disabled": True, "disabled_at": datetime.now(UTC)}
    )
    print(f"client {args.client_id} disabled (cached for up to 60s per instance)")


def gmail_watch(args: argparse.Namespace) -> None:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from .clients.gmail import GMAIL_SCOPES

    info = json.loads(_secret(args.project, "gmail-oauth-json"))
    creds = Credentials(
        token=None,
        refresh_token=info["refresh_token"],
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106 - public endpoint
        scopes=GMAIL_SCOPES,
    )
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    resp = (
        gmail.users()
        .watch(userId="me", body={"topicName": args.topic, "labelIds": ["INBOX"]})
        .execute()
    )
    print(f"watch active: historyId={resp['historyId']} expiration(ms)={resp['expiration']}")
    print("Gmail watches expire after 7 days; re-run this daily (e.g. Cloud Scheduler).")


def _id(value: str) -> str:
    if not _ID.match(value):
        raise argparse.ArgumentTypeError("ids must match [A-Za-z0-9_-]{1,64}")
    return value


def main(argv: list[str] | None = None) -> None:
    import os

    parser = argparse.ArgumentParser(prog="acs-admin")
    parser.add_argument("--project", default=os.environ.get("ACS_PROJECT_ID"), required=False)
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("register-merchant")
    p.add_argument("--merchant", type=_id, required=True)
    p.add_argument("--domain", action="append", required=True)
    p.set_defaults(fn=register_merchant)

    p = sub.add_parser("set-claims")
    p.add_argument("--uid", required=True)
    p.add_argument("--merchant", type=_id, required=True)
    p.add_argument("--role", choices=[r.value for r in Role], required=True)
    p.set_defaults(fn=set_claims)

    p = sub.add_parser("create-client")
    p.add_argument("--merchant", type=_id, required=True)
    p.add_argument(
        "--kind",
        choices=[PrincipalKind.SHOPPER_AGENT.value, PrincipalKind.B2B_PARTNER.value],
        required=True,
    )
    p.add_argument("--ucp-profile")
    p.add_argument("--display")
    p.set_defaults(fn=create_client)

    p = sub.add_parser("disable-client")
    p.add_argument("--client-id", required=True)
    p.set_defaults(fn=disable_client)

    p = sub.add_parser("gmail-watch")
    p.add_argument("--topic", required=True)
    p.set_defaults(fn=gmail_watch)

    args = parser.parse_args(argv)
    if not args.project:
        parser.error("--project or ACS_PROJECT_ID is required")
    args.fn(args)


if __name__ == "__main__":
    main()
