"""快速测试脚本 -- 完整端到端验证。

模拟游戏服务器发送 Webhook -> 后台执行分析 -> 查询分析结果。

前置条件:
    SIMULATE_GAME_SERVER=1 uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000

用法:
    uv run python tests/quick_test.py
"""

import asyncio
import json
import time

import httpx

BASE_URL = "http://localhost:8000"
API_KEY = "gap_test_alpha_key_002"


async def send_event(user_id: str, event_type: str, snapshot: dict | None = None) -> httpx.Response:
    payload = {"user_id": user_id, "event_type": event_type, "timestamp": time.time()}
    if snapshot is not None:
        payload["snapshot"] = snapshot
    async with httpx.AsyncClient() as client:
        return await client.post(
            f"{BASE_URL}/webhooks/player-event",
            json=payload,
            headers={"Content-Type": "application/json", "X-API-Key": API_KEY},
            timeout=30.0,
        )


async def query_latest(user_id: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(
            f"{BASE_URL}/api/v1/analysis/{user_id}/latest",
            headers={"X-API-Key": API_KEY},
            timeout=10.0,
        )


async def query_history(user_id: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(
            f"{BASE_URL}/api/v1/analysis/{user_id}/history",
            headers={"X-API-Key": API_KEY},
            timeout=10.0,
        )


async def wait_for_result(user_id: str, timeout: float = 120, interval: float = 3) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = await query_latest(user_id)
        if resp.status_code == 200:
            return resp.json()
        await asyncio.sleep(interval)
    return None


def _print_result(result: dict):
    output = result.get("output", {})
    profile = output.get("player_profile", {})
    actions = output.get("recommended_actions", [])

    print(f"  --- analysis result ---")
    print(f"  analyzed_at: {result.get('analyzed_at', 'N/A')}")
    if profile:
        print(f"  playstyle:   {profile.get('playstyle', 'N/A')}")
        print(f"  engagement:  {profile.get('engagement_level', 'N/A')}")
        print(f"  goals:       {profile.get('current_goal', [])}")
        print(f"  bottlenecks: {profile.get('bottlenecks', [])}")
    if actions:
        print(f"  actions ({len(actions)}):")
        for i, a in enumerate(actions):
            print(f"    {i + 1}. [{a.get('priority')}] {a.get('action_type')}: {a.get('reason')}")
    if not profile and not actions:
        print(f"  raw: {json.dumps(output, ensure_ascii=False)[:500]}")


# ---- test cases ----


async def test_full_analysis():
    print("=" * 60)
    print("Test 1: full offline analysis pipeline")
    print("=" * 60)

    user_id = "e2e_player_001"
    resp = await send_event(user_id, "offline")
    print(f"  webhook: {resp.status_code} {resp.json()}")

    if resp.json().get("status") != "scheduled":
        print("  [FAIL] not scheduled")
        return False

    print("  waiting for analysis (up to 120s)...")
    result = await wait_for_result(user_id)
    if result is None:
        print("  [FAIL] timeout")
        return False

    print("  [PASS] analysis completed")
    _print_result(result)
    return True


async def test_debounce():
    print("\n" + "=" * 60)
    print("Test 2: debounce")
    print("=" * 60)

    user_id = "e2e_debounce_001"
    r1 = await send_event(user_id, "offline")
    s1 = r1.json().get("status")
    r2 = await send_event(user_id, "offline")
    s2 = r2.json().get("status")
    print(f"  1st offline: {s1}")
    print(f"  2nd offline: {s2}")

    if s2 == "debounced":
        print("  [PASS]")
        return True
    print(f"  [FAIL] expected debounced, got {s2}")
    return False


async def test_online_cancel():
    print("\n" + "=" * 60)
    print("Test 3: online cancel")
    print("=" * 60)

    user_id = "e2e_cancel_001"
    r1 = await send_event(user_id, "offline")
    print(f"  offline: {r1.json()}")
    r2 = await send_event(user_id, "online")
    s2 = r2.json().get("status")
    print(f"  online:  {r2.json()}")

    if s2 == "cancelled":
        print("  [PASS]")
        return True
    print(f"  [FAIL] expected cancelled, got {s2}")
    return False


async def test_query_history():
    print("\n" + "=" * 60)
    print("Test 4: query history")
    print("=" * 60)

    user_id = "e2e_player_001"
    resp = await query_history(user_id)
    if resp.status_code == 200:
        data = resp.json()
        print(f"  records: {data.get('count', 0)}")
        for h in data.get("history", []):
            print(f"    - {h.get('analyzed_at')}")
        print("  [PASS]")
        return True
    print(f"  [FAIL] {resp.status_code}")
    return False


async def main():
    print("myAgent v2.0 -- E2E simulation test")
    print()

    tests = [
        ("full analysis", test_full_analysis),
        ("debounce", test_debounce),
        ("online cancel", test_online_cancel),
        ("query history", test_query_history),
    ]

    results = []
    for name, fn in tests:
        try:
            ok = await fn()
            results.append((name, ok, None))
        except Exception as e:
            results.append((name, False, str(e)))

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    passed = 0
    for name, ok, err in results:
        tag = "PASS" if ok and not err else "FAIL"
        detail = f" -- {err}" if err else ""
        print(f"  [{tag}] {name}{detail}")
        if ok and not err:
            passed += 1
    print(f"\n  {passed}/{len(results)} passed")


if __name__ == "__main__":
    asyncio.run(main())
