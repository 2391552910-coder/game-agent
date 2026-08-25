from pathlib import Path


async def test_gateway_monitor_page_and_assets_are_public(client) -> None:
    page = await client.get("/gateway")
    stylesheet = await client.get("/gateway/assets/app.css")
    script = await client.get("/gateway/assets/app.js")

    assert page.status_code == 200
    assert stylesheet.status_code == 200
    assert script.status_code == 200
    assert "Gateway 调用监控" in page.text
    assert "托管 Agent" in page.text
    assert "当前已加载" in page.text
    assert 'id="timeline"' in page.text
    assert 'id="detail-panel"' in page.text
    assert 'id="conversation-panel"' in page.text


def test_gateway_monitor_client_supports_sse_recovery_and_manual_refresh() -> None:
    script = Path("src/api/static/gateway_monitor/app.js").read_text(encoding="utf-8")

    assert "new EventSource" in script
    assert "lastEventId" in script
    assert "scheduleReconnect" in script
    assert 'addEventListener("change"' in script
    assert 'getElementById("refresh-button")' in script
    assert "streamCursor" in script


def test_gateway_monitor_client_renders_errors_tokens_and_conversations() -> None:
    script = Path("src/api/static/gateway_monitor/app.js").read_text(encoding="utf-8")

    assert "inputTokens" in script
    assert "outputTokens" in script
    assert "totalTokens" in script
    assert "usageMissingCalls" in script
    assert "errorDetail" in script
    assert "renderConversation" in script
    assert "chat_received" in script
    assert "requestDirection" in script
    assert "responseDirection" in script
    assert 'record.kind === "skill"' in script


def test_gateway_monitor_styles_include_responsive_and_accessible_states() -> None:
    stylesheet = Path("src/api/static/gateway_monitor/app.css").read_text(encoding="utf-8")

    assert "@media (max-width: 760px)" in stylesheet
    assert ":focus-visible" in stylesheet
    assert "prefers-reduced-motion" in stylesheet
    assert "--color-error" in stylesheet
    assert "--color-chat" in stylesheet
    assert ".topbar { padding: 12px 16px; flex-wrap: wrap; }" in stylesheet
