(function () {
  "use strict";

  const state = {
    records: new Map(),
    selectedId: null,
    kind: "all",
    search: "",
    olderCursor: "",
    streamCursor: "",
    lastEventId: "",
    source: null,
    reconnectTimer: null,
    reconnectAttempt: 0,
    hasMore: false,
  };

  const el = (id) => document.getElementById(id);
  const timeline = el("timeline");
  const apiBase = "/api/gateway/v2/monitor";
  const chatEventTypes = new Set(["chat_received", "nearby_friend_chat_requested", "chat_send_result"]);

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>\"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function formatJson(value) {
    if (value === null || value === undefined) return "暂无内容";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }

  function number(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN") : "--"; }

  function normalizedKind(record) {
    if (record.kind === "chat" || chatEventTypes.has(record.eventType) || String(record.eventType || "").includes("chat")) return "chat";
    if (record.error || ["failed", "retryable_failed", "dead_letter", "rejected", "manual"].includes(record.status)) return "error";
    if (record.kind === "session" || ["session_started", "session_stopped"].includes(record.eventType)) return "session";
    return record.kind || "call";
  }

  function recordMatches(record) {
    const kind = normalizedKind(record);
    if (state.kind !== "all" && kind !== state.kind) return false;
    if (!state.search) return true;
    const haystack = JSON.stringify(record).toLowerCase();
    return haystack.includes(state.search.toLowerCase());
  }

  function sortedRecords() {
    return Array.from(state.records.values()).filter(recordMatches).sort((a, b) => {
      const timeA = Date.parse(a.timestamp || a.occurredAt || a.createdAt || "") || 0;
      const timeB = Date.parse(b.timestamp || b.occurredAt || b.createdAt || "") || 0;
      return timeB - timeA;
    });
  }

  function statusClass(record) {
    const kind = normalizedKind(record);
    if (kind === "chat") return "chat";
    if (record.error || ["failed", "retryable_failed", "dead_letter", "rejected", "manual"].includes(record.status)) return "error";
    return "success";
  }

  function title(record) {
    if (record.title) return record.title;
    if (record.eventType) return record.direction === "agent_to_gateway" ? `托管 Agent → ${record.eventType}` : `Gateway → ${record.eventType}`;
    if (record.skillName) return `托管 Agent → ${record.skillName}`;
    return record.kind === "chat" ? "对话消息" : "Gateway 调用";
  }

  function renderTimeline() {
    const records = sortedRecords();
    el("record-count").textContent = String(records.length);
    timeline.innerHTML = "";
    el("empty-state").hidden = records.length !== 0;
    el("timeline-state").textContent = records.length ? `${records.length} 条记录` : "等待记录";
    el("load-more-button").hidden = !state.hasMore;
    records.forEach((record) => {
      const kind = normalizedKind(record);
      const status = statusClass(record);
      const button = document.createElement("button");
      button.type = "button";
      button.className = `timeline-entry is-${status}${record.id === state.selectedId ? " is-selected" : ""}`;
      button.dataset.recordId = record.id;
      button.innerHTML = `<div class="entry-top"><span class="entry-title">${escapeHtml(title(record))}</span><time class="entry-time">${escapeHtml(formatTime(record.timestamp || record.occurredAt || record.createdAt))}</time></div><div class="entry-meta"><span>${escapeHtml(record.sessionId || "无 session")}</span><span>·</span><span>${escapeHtml(record.traceId || record.decisionId || record.eventId || "无 trace")}</span></div><div class="entry-footer"><span class="badge ${status}">${escapeHtml(displayStatus(record.status, kind))}</span><span>${escapeHtml(entryHint(record))}</span></div>`;
      button.addEventListener("click", () => selectRecord(record.id));
      timeline.appendChild(button);
    });
  }

  function formatTime(value) {
    if (!value) return "--:--:--";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value).slice(11, 19) : date.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function displayStatus(status, kind) {
    if (status === "accepted") return "已接受";
    if (status === "succeeded") return "成功";
    if (status === "failed" || status === "retryable_failed" || status === "dead_letter") return "异常";
    if (status === "rejected") return "已拒绝";
    if (kind === "chat") return "对话";
    return status || "已记录";
  }

  function entryHint(record) {
    const tokens = record.tokenUsage || record.tokens;
    if (record.totalTokens !== undefined || tokens?.totalTokens !== undefined) return `${number(record.totalTokens ?? tokens.totalTokens)} Token`;
    if (record.error?.category || record.errorCategory) return record.error?.category || record.errorCategory;
    if (record.text || record.message) return "点击查看内容";
    return record.responseStatus || "点击查看详情";
  }

  function selectRecord(id) {
    state.selectedId = id;
    renderTimeline();
    renderDetail(state.records.get(id));
  }

  function renderDetail(record) {
    if (!record) { el("detail-empty").hidden = false; el("detail-content").hidden = true; return; }
    el("detail-empty").hidden = true;
    el("detail-content").hidden = false;
    const kind = normalizedKind(record);
    el("detail-summary").innerHTML = `<div class="summary-title">${escapeHtml(title(record))}</div><div class="summary-grid"><span>时间<strong>${escapeHtml(formatDateTime(record.timestamp || record.occurredAt || record.createdAt))}</strong></span><span>状态<strong>${escapeHtml(displayStatus(record.status, kind))}</strong></span><span>Session<strong>${escapeHtml(record.sessionId || "--")}</strong></span><span>Trace<strong>${escapeHtml(record.traceId || "--")}</strong></span><span>事件<strong>${escapeHtml(record.eventId || "--")}</strong></span><span>决策<strong>${escapeHtml(record.decisionId || "--")}</strong></span></div>`;
    renderTokens(record);
    renderConversation(record);
    el("request-payload").textContent = formatJson(record.request ?? record.requestBody ?? record.eventBody ?? record.payload);
    el("response-payload").textContent = formatJson(record.response ?? record.responseBody ?? record.result);
    el("request-direction").textContent = requestDirection(record);
    el("response-direction").textContent = responseDirection(record);
    renderError(record);
  }

  function renderTokens(record) {
    const tokens = record.tokenUsage || record.tokens || record;
    const hasTokens = [tokens.inputTokens, tokens.outputTokens, tokens.totalTokens].some((value) => value !== null && value !== undefined);
    const missing = Number(tokens.usageMissingCalls || record.usageMissingCalls || 0);
    el("token-summary").innerHTML = `<div class="token-cell"><strong>${number(tokens.inputTokens)}</strong><span>输入 Token</span></div><div class="token-cell"><strong>${number(tokens.outputTokens)}</strong><span>输出 Token</span></div><div class="token-cell"><strong>${number(tokens.totalTokens)}</strong><span>总 Token</span></div>${hasTokens ? `<span class="token-missing">模型调用 ${escapeHtml(tokens.modelCalls ?? record.modelCalls ?? "--")} 次 · ${missing ? `有 ${missing} 次未上报用量` : "用量已上报"}</span>` : "<span class=\"token-missing\">本次调用未上报 Token 用量</span>"}`;
  }

  function renderConversation(record) {
    const conversation = record.conversation || record.chat || (record.content ? { messages: [{ role: record.direction === "outbound" ? "托管 Agent" : "Gateway", content: record.content }] } : null);
    const panel = el("conversation-panel");
    if (!conversation || (!conversation.incoming && !conversation.outgoing && !conversation.messages)) { panel.hidden = true; panel.innerHTML = ""; return; }
    const lines = Array.isArray(conversation.messages) ? conversation.messages : [{ role: "Gateway", content: conversation.incoming }, { role: "托管 Agent", content: conversation.outgoing }];
    panel.hidden = false;
    panel.innerHTML = `<div class="conversation-title">对话内容</div>${lines.filter((line) => line && line.content).map((line) => `<div class="chat-line ${String(line.role).includes("Agent") ? "agent" : ""}"><span class="chat-role">${escapeHtml(line.role || "消息")}</span>${escapeHtml(line.content)}</div>`).join("")}`;
  }

  function requestDirection(record) {
    if (record.kind === "skill") return "托管 Agent → Gateway";
    if (record.direction === "outbound") return "托管 Agent → Gateway";
    if (record.direction === "system") return "托管 Agent 内部";
    return "Gateway → 托管 Agent";
  }

  function responseDirection(record) {
    if (record.direction === "outbound") return "Gateway → 托管 Agent";
    if (record.direction === "system") return "托管 Agent 内部";
    return "托管 Agent → Gateway";
  }

  function renderError(record) {
    const rawErrorDetail = record.errorDetail;
    const detail = record.error || (rawErrorDetail ? { message: rawErrorDetail } : null) || (record.errorCategory ? { stage: record.errorStage, category: record.errorCategory, message: record.errorMessage } : null);
    const block = el("error-block");
    if (!detail) { block.hidden = true; el("error-detail").innerHTML = ""; return; }
    block.hidden = false;
    el("error-detail").innerHTML = Object.entries(detail).filter(([, value]) => value !== null && value !== undefined && value !== "").map(([key, value]) => `<dt>${escapeHtml(errorLabel(key))}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  }

  function errorLabel(key) { return ({ stage: "阶段", category: "类别", message: "详情", httpStatus: "HTTP 状态", retryable: "可重试" }[key] || key); }
  function formatDateTime(value) { const date = new Date(value || ""); return Number.isNaN(date.getTime()) ? String(value || "--") : date.toLocaleString("zh-CN", { hour12: false }); }

  function ingest(payload, options) {
    const config = options || {};
    const records = Array.isArray(payload) ? payload : payload.items || payload.records || [];
    if (config.replace) state.records.clear();
    records.forEach((record) => { if (record && record.id) state.records.set(String(record.id), record); });
    if (payload && !Array.isArray(payload) && !config.live) {
      state.olderCursor = payload.nextCursor || payload.cursor || state.olderCursor;
      if (payload.streamCursor && (config.replace || !state.streamCursor)) state.streamCursor = payload.streamCursor;
      state.hasMore = Boolean(payload.hasMore);
    }
    el("timeline").setAttribute("aria-busy", "false");
    el("last-sync").textContent = `同步于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    updateMetrics();
    renderTimeline();
    if (state.selectedId) renderDetail(state.records.get(state.selectedId));
  }

  function updateMetrics() {
    const all = Array.from(state.records.values());
    el("metric-total").textContent = number(all.length);
    el("metric-errors").textContent = number(all.filter((record) => normalizedKind(record) === "error").length);
    el("metric-chat").textContent = number(all.filter((record) => normalizedKind(record) === "chat").length);
    el("metric-tokens").textContent = number(all.reduce((sum, record) => sum + Number(record.totalTokens ?? record.tokenUsage?.totalTokens ?? record.tokens?.totalTokens ?? 0), 0));
  }

  async function queryRecords(cursor) {
    const params = new URLSearchParams({ limit: "80" });
    if (cursor) params.set("cursor", cursor);
    const response = await fetch(`${apiBase}?${params.toString()}`, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`monitor query failed: ${response.status}`);
    return response.json();
  }

  async function refresh() {
    ingest(await queryRecords(""), { replace: true });
  }

  async function loadMore() {
    if (!state.olderCursor) return;
    ingest(await queryRecords(state.olderCursor));
  }

  function setConnection(status, label) { const target = el("connection-state"); target.className = `connection-state is-${status}`; target.querySelector("span:last-child").textContent = label; }

  function closeStream() { if (state.source) { state.source.close(); state.source = null; } }
  function scheduleReconnect() { if (state.reconnectTimer) return; const delay = Math.min(1_000 * (2 ** state.reconnectAttempt), 15_000); state.reconnectAttempt += 1; state.reconnectTimer = setTimeout(() => { state.reconnectTimer = null; openStream(); }, delay); }
  function openStream() {
    closeStream();
    const params = new URLSearchParams();
    const resumeCursor = state.lastEventId || state.streamCursor;
    if (resumeCursor) params.set("cursor", resumeCursor);
    state.source = new EventSource(`${apiBase}/stream?${params.toString()}`);
    state.source.addEventListener("open", () => { state.reconnectAttempt = 0; setConnection("connected", "SSE 已连接"); });
    const handleRecord = (event) => {
      state.lastEventId = event.lastEventId || state.lastEventId;
      state.streamCursor = state.lastEventId || state.streamCursor;
      try { ingest([JSON.parse(event.data)], { live: true }); } catch (_) { setConnection("error", "数据格式错误"); }
    };
    state.source.addEventListener("record", handleRecord);
    state.source.addEventListener("change", handleRecord);
    state.source.onerror = () => { setConnection("error", "连接中断，重连中"); closeStream(); scheduleReconnect(); };
  }

  const refreshButton = document.getElementById("refresh-button");
  refreshButton.addEventListener("click", () => refresh().catch(() => setConnection("error", "刷新失败")));
  el("close-detail-button").addEventListener("click", () => { state.selectedId = null; renderTimeline(); el("detail-empty").hidden = false; el("detail-content").hidden = true; });
  el("search-input").addEventListener("input", (event) => { state.search = event.target.value.trim(); renderTimeline(); });
  document.querySelectorAll(".filter-button").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll(".filter-button").forEach((item) => item.classList.remove("is-active")); button.classList.add("is-active"); state.kind = button.dataset.kind; renderTimeline(); }));
  el("load-more-button").addEventListener("click", () => loadMore().catch(() => setConnection("error", "加载失败")));
  refresh().catch(() => setConnection("error", "查询失败")).finally(openStream);
}());
