"""Operator CLI: load the SKU catalog (with embeddings) and map buyers to Odoo partners.

    acs-messy-admin load-skus --merchant m1 --csv catalog.csv
    acs-messy-admin set-customer --merchant m1 --ref buyer-one.example --partner-id 42 \
        --name "Buyer One" --terms 30

catalog.csv columns: sku,name,description,uom,list_price,currency,odoo_product_id,aliases
(aliases are ';'-separated buyer codes; list_price is in major units, e.g. 12.50).
Requires ACS_PROJECT_ID and GOOGLE_API_KEY; runs with the operator's ADC credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path
from typing import Any

from .catalog import sku_key
from .config import get_settings
from .models import to_minor

_MERCHANT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BATCH = 100


def catalog_rows(path: Path, merchant_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for n, row in enumerate(csv.DictReader(handle), 2):
            sku = (row.get("sku") or "").strip()
            currency = (row.get("currency") or "USD").strip().upper()
            if not sku or "/" in sku or len(sku) > 64:
                raise SystemExit(f"line {n}: invalid sku")
            product = (row.get("odoo_product_id") or "").strip()
            aliases = [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()]
            rows.append(
                {
                    "merchant_id": merchant_id,
                    "sku": sku,
                    "sku_key": sku_key(sku),
                    "alias_keys": sorted({sku_key(a) for a in aliases} - {""}),
                    "name": (row.get("name") or "").strip()[:200],
                    "description": (row.get("description") or "").strip()[:1000],
                    "uom": (row.get("uom") or "").strip()[:32],
                    "list_price_minor": to_minor(float(row.get("list_price") or 0), currency),
                    "currency": currency,
                    "odoo_product_id": int(product) if product.isdigit() else None,
                }
            )
    return rows


async def load_skus(merchant_id: str, path: Path) -> int:
    from acs_common.gemini import embed, genai_client
    from google.cloud import firestore
    from google.cloud.firestore_v1.vector import Vector

    settings = get_settings()
    rows = catalog_rows(path, merchant_id)
    db = firestore.AsyncClient(project=settings.project_id, database=settings.firestore_database)
    client = genai_client()
    for start in range(0, len(rows), _BATCH):
        chunk = rows[start : start + _BATCH]
        texts = [f"{r['name']}. {r['description']} ({r['sku']}) [{r['uom']}]" for r in chunk]
        vectors = await embed(
            client,
            settings.embedding_model,
            texts,
            task_type="RETRIEVAL_DOCUMENT",
            dimensions=settings.embedding_dimensions,
        )
        batch = db.batch()
        for row, vector in zip(chunk, vectors, strict=True):
            ref = db.collection("skus").document(f"{merchant_id}:{row['sku_key']}")
            batch.set(ref, {**row, "embedding": Vector(vector)})
        await batch.commit()
        print(f"loaded {start + len(chunk)}/{len(rows)}")
    return len(rows)


async def set_customer(
    merchant_id: str, ref: str, partner_id: int, name: str, terms: int | None
) -> None:
    from google.cloud import firestore

    settings = get_settings()
    db = firestore.AsyncClient(project=settings.project_id, database=settings.firestore_database)
    await (
        db.collection("customers")
        .document(f"{merchant_id}:{ref.lower()}")
        .set(
            {
                "merchant_id": merchant_id,
                "ref": ref.lower(),
                "odoo_partner_id": partner_id,
                "name": name[:200],
                "payment_terms_days": terms,
            }
        )
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="acs-messy-admin")
    sub = parser.add_subparsers(dest="command", required=True)
    skus = sub.add_parser("load-skus")
    skus.add_argument("--merchant", required=True)
    skus.add_argument("--csv", required=True, type=Path)
    cust = sub.add_parser("set-customer")
    cust.add_argument("--merchant", required=True)
    cust.add_argument("--ref", required=True, help="sender domain, partner client id or buyer ref")
    cust.add_argument("--partner-id", required=True, type=int)
    cust.add_argument("--name", default="")
    cust.add_argument("--terms", type=int, default=None, help="agreed payment terms (days)")
    args = parser.parse_args(argv)

    if not get_settings().project_id:
        sys.exit("ACS_PROJECT_ID is required")
    if not _MERCHANT.match(args.merchant):
        sys.exit("invalid --merchant")
    if args.command == "load-skus":
        count = asyncio.run(load_skus(args.merchant, args.csv))
        print(f"{count} SKUs loaded for {args.merchant}")
    else:
        if args.partner_id <= 0 or "/" in args.ref or len(args.ref) > 128:
            sys.exit("invalid --partner-id or --ref")
        asyncio.run(set_customer(args.merchant, args.ref, args.partner_id, args.name, args.terms))
        print(f"customer {args.ref.lower()} -> Odoo partner {args.partner_id}")


if __name__ == "__main__":
    main()
