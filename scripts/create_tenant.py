#!/usr/bin/env python3
"""Create a tenant with a freshly generated API key.

Generates a random `tok_...` key, stores only its SHA-256 hash in the
tenants table, and prints the plaintext key exactly once. There is no way
to recover the key later — store it in a secret manager immediately.

Usage:
    python scripts/create_tenant.py --id genie-platform --name "Genie Platform" --budget 1000
    python scripts/create_tenant.py --id acme --name "Acme Corp"            # default $500 budget
    python scripts/create_tenant.py --id acme --name "Acme Corp" --rotate   # rotate an existing tenant's key

Requires DATABASE_URL in the environment (or .env).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import secrets
import sys


def generate_key() -> tuple[str, str]:
    """Return (plaintext_key, sha256_hash)."""
    key = "tok_" + secrets.token_urlsafe(24)
    return key, hashlib.sha256(key.encode("utf-8")).hexdigest()


async def create_tenant(
    tenant_id: str,
    name: str,
    budget: float,
    rotate: bool,
) -> str:
    from proxy.config import settings

    import asyncpg

    key, key_hash = generate_key()

    conn = await asyncpg.connect(settings.database_url)
    try:
        existing = await conn.fetchrow(
            "SELECT id FROM tenants WHERE id = $1", tenant_id
        )
        if existing and not rotate:
            raise SystemExit(
                f"tenant '{tenant_id}' already exists — use --rotate to issue a new key"
            )
        if existing:
            await conn.execute(
                "UPDATE tenants SET api_key_hash = $2, name = $3, monthly_budget_usd = $4 WHERE id = $1",
                tenant_id,
                key_hash,
                name,
                budget,
            )
        else:
            await conn.execute(
                """
                INSERT INTO tenants (id, name, api_key_hash, monthly_budget_usd)
                VALUES ($1, $2, $3, $4)
                """,
                tenant_id,
                name,
                key_hash,
                budget,
            )
    finally:
        await conn.close()

    return key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="tenant id (kebab-case)")
    parser.add_argument("--name", required=True, help="display name")
    parser.add_argument("--budget", type=float, default=500.0, help="monthly budget USD")
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="replace the key of an existing tenant (invalidates the old key)",
    )
    args = parser.parse_args()

    key = asyncio.run(create_tenant(args.id, args.name, args.budget, args.rotate))

    action = "rotated" if args.rotate else "created"
    print(f"tenant '{args.id}' {action}.")
    print()
    print("API key (shown once — store it securely, only the hash is in the DB):")
    print()
    print(f"  {key}")
    print()
    print("Note: the proxy caches key lookups for 5 minutes; after a rotation the")
    print("old key stays valid until the cache entry expires or the proxy restarts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
