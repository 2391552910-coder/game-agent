from __future__ import annotations


async def test_metrics_route_exposes_gateway_v2_llm_metrics(client) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "myagent_llm_calls_total" in response.text
    assert "myagent_llm_tokens_total" in response.text
