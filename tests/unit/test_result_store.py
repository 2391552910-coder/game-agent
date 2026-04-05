"""结果存储测试。"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.core.infrastructure.result_store import _snapshot_hash, store_analysis


class TestSnapshotHash:
    def test_deterministic(self):
        snap = {"a": 1, "b": "hello"}
        h1 = _snapshot_hash(snap)
        h2 = _snapshot_hash(snap)
        assert h1 == h2

    def test_order_independent(self):
        """sort_keys=True 确保 key 顺序不影响哈希。"""
        h1 = _snapshot_hash({"a": 1, "b": 2})
        h2 = _snapshot_hash({"b": 2, "a": 1})
        assert h1 == h2

    def test_different_data_different_hash(self):
        h1 = _snapshot_hash({"level": 25})
        h2 = _snapshot_hash({"level": 26})
        assert h1 != h2

    def test_sha256_length(self):
        h = _snapshot_hash({"key": "value"})
        assert len(h) == 64  # SHA-256 hex digest


class TestStoreAnalysis:
    @pytest.mark.asyncio
    async def test_calls_db_insert(self, mock_session):
        session, ctx = mock_session
        result = await store_analysis(
            tenant_id="t-001",
            user_id="u-001",
            snapshot={"level": 25},
            output={"player_profile": {}, "recommended_actions": []},
        )

        # 验证 execute 被调用
        session.execute.assert_called_once()
        # 验证参数包含正确的 tenant_id
        call_kwargs = session.execute.call_args[0][1]
        assert call_kwargs["tenant_id"] == "t-001"
        assert call_kwargs["user_id"] == "u-001"
        assert "snapshot_hash" in call_kwargs
        assert "output_json" in call_kwargs

    @pytest.mark.asyncio
    async def test_output_json_serialization(self, mock_session):
        session, ctx = mock_session
        output = {"key": "中文值"}
        await store_analysis("t", "u", {"a": 1}, output)

        call_kwargs = session.execute.call_args[0][1]
        parsed = json.loads(call_kwargs["output_json"])
        assert parsed["key"] == "中文值"
