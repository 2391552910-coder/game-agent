from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from uuid import UUID

import sqlalchemy as sa

from scripts.v2_e2e_common import open_verified_test_engine

_GATEWAY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def parse_seed_identity(tenant_id: str, gateway_id: str) -> tuple[UUID, str]:
    try:
        parsed_tenant_id = UUID(tenant_id)
    except (TypeError, ValueError) as error:
        raise ValueError("tenant-id must be a UUID") from error
    if not _GATEWAY_ID_PATTERN.fullmatch(gateway_id):
        raise ValueError("gateway-id must be a non-empty bounded protocol ID")
    return parsed_tenant_id, gateway_id


async def seed_test_tenant(tenant_id: UUID, gateway_id: str) -> None:
    engine = await open_verified_test_engine()
    try:
        digest = hashlib.sha256(f"{tenant_id}:{gateway_id}".encode()).hexdigest()[:24]
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO tenants (id, user_id, api_key, is_active, is_admin)
                    VALUES (:tenant_id, :user_id, :api_key, true, false)
                    ON CONFLICT (id) DO UPDATE
                    SET user_id = EXCLUDED.user_id,
                        is_active = true,
                        is_admin = false
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": f"gateway-v2-e2e-{digest}",
                    "api_key": f"e2e_{digest}",
                },
            )
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--gateway-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        tenant_id, gateway_id = parse_seed_identity(args.tenant_id, args.gateway_id)
        asyncio.run(seed_test_tenant(tenant_id, gateway_id))
    except Exception as error:
        print(json.dumps({"success": False, "category": type(error).__name__}, separators=(",", ":")))
        return 1
    print(
        json.dumps(
            {"success": True, "tenantId": str(tenant_id), "gatewayId": gateway_id},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
