from __future__ import annotations

import hashlib
import json
from typing import Any

from src.core.integration.llm_gateway_v2.contracts import GatewayV2Event


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a JSON value into the stable UTF-8 representation used for hashes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_event_bytes(event: GatewayV2Event) -> bytes:
    return canonical_json_bytes(event.model_dump(mode="json"))


def event_content_hash(gateway_id: str, event: GatewayV2Event) -> str:
    """Hash only the gateway identity and immutable event contract fields."""
    content = {
        "gatewayId": gateway_id,
        "event": event.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()
