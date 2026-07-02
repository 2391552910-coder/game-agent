"""Exact-match RAG supplement for structured game knowledge."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_:-]+|[\u4e00-\u9fff]+")
_STOP_PHRASES = (
    "在哪里",
    "多少钱",
    "是多少",
    "是什么",
    "需要什么",
    "消耗什么",
    "坐标是多少",
    "坐标",
    "价格",
    "哪里",
    "多少",
    "什么",
    "怎么",
    "如何",
    "吗",
    "呢",
    "的",
)


@dataclass(frozen=True)
class ExactMatchHit:
    key: str
    score: float
    content: str
    file_path: str | None = None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def extract_query_terms(query: str) -> list[str]:
    """Extract domain terms suitable for exact chunk matching."""
    raw_terms: list[str] = []
    for token in _TOKEN_RE.findall(query):
        cleaned = token
        for stop_phrase in _STOP_PHRASES:
            cleaned = cleaned.replace(stop_phrase, " ")
        for part in cleaned.split():
            part = part.strip()
            if len(part) >= 2:
                raw_terms.append(part)

    expanded_terms: list[str] = []
    for term in raw_terms:
        expanded_terms.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", term) and len(term) > 4:
            for size in range(min(8, len(term) - 1), 1, -1):
                for start in range(0, len(term) - size + 1):
                    expanded_terms.append(term[start : start + size])

    seen: set[str] = set()
    terms: list[str] = []
    for term in sorted(expanded_terms, key=len, reverse=True):
        normalized = normalize_text(term)
        if normalized in seen or normalized in {normalize_text(item) for item in _STOP_PHRASES}:
            continue
        seen.add(normalized)
        terms.append(term)
    return terms


def _load_chunk_payload(raw_value: str | bytes | None) -> dict[str, Any] | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8")
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return {"content": raw_value}
    if not isinstance(payload, dict):
        return None
    return payload


def score_exact_match(query_terms: list[str], content: str) -> float:
    normalized_content = normalize_text(content)
    score = 0.0
    for term in query_terms:
        normalized_term = normalize_text(term)
        if not normalized_term or normalized_term not in normalized_content:
            continue
        score += len(normalized_term)
        if re.fullmatch(r"[A-Za-z0-9_:-]+", term):
            score += 4
        if any(marker in normalized_term for marker in ("id", "act_", "item_", "gym_", "sports_")):
            score += 4
    return score


def format_exact_matches(hits: list[ExactMatchHit]) -> str:
    if not hits:
        return ""

    lines = ["【精确匹配RAG补充】"]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. 来源: {hit.file_path or hit.key}; score={hit.score:.1f}")
        lines.append(hit.content.strip())
    return "\n".join(lines)


async def retrieve_exact_rag_context(query: str) -> str:
    """Scan text chunks and return high-confidence exact matches."""
    from redis.asyncio import Redis

    from src.config import settings

    if not getattr(settings, "rag_exact_match_enabled", True):
        return ""

    terms = extract_query_terms(query)
    if not terms:
        return ""

    hits: list[ExactMatchHit] = []
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async for key in redis.scan_iter(match="text_chunks:*", count=1000):
            payload = _load_chunk_payload(await redis.get(key))
            if not payload:
                continue
            content = str(payload.get("content") or payload.get("text") or "")
            if not content:
                continue
            score = score_exact_match(terms, content)
            if score <= 0:
                continue
            hits.append(
                ExactMatchHit(
                    key=str(key),
                    score=score,
                    content=content,
                    file_path=payload.get("file_path"),
                )
            )
    except Exception as exc:
        logger.warning("[rag_exact_match] 精确匹配检索失败: %s", exc)
        return ""
    finally:
        await redis.aclose()

    hits.sort(key=lambda hit: (-hit.score, hit.key))
    top_k = getattr(settings, "rag_exact_match_top_k", 5)
    return format_exact_matches(hits[:top_k])
