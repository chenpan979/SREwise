/**
 * API client + SSE wrapper.
 *
 * 所有后端调用都走这里,统一错误处理与超时。
 */

const BASE = "";  // 同源,不需要前缀

/** 通用 JSON GET。 */
export async function getJSON(path, params = {}) {
  const url = new URL(BASE + path, location.origin);
  Object.entries(params).forEach(([k, v]) => {
    if (v === undefined || v === null || v === "") return;
    url.searchParams.set(k, v);
  });
  const res = await fetch(url, { headers: { "Accept": "application/json" } });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text || path}`);
  }
  return res.json();
}

/** 通用 JSON POST。 */
export async function postJSON(path, body = {}) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text || path}`);
  }
  return res.json();
}

/**
 * SSE consumer that handles both `event-stream` standard messages and
 * the `event: <name>` named events the FastAPI sse-starlette emits.
 *
 * Returns an async iterator yielding parsed objects:
 *   { event: "message"|<name>, data: <parsed JSON or string> }
 *
 * Use the returned `controller.abort()` to cancel.
 */
export async function* sseStream(path, { method = "POST", body = null } = {}) {
  const controller = new AbortController();
  const opts = {
    method,
    signal: controller.signal,
    headers: {
      "Accept": "text/event-stream",
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
  };
  if (body) opts.body = JSON.stringify(body);

  const res = await fetch(BASE + path, opts);
  if (!res.ok) {
    throw new Error(`SSE ${path} ${res.status} ${res.statusText}`);
  }
  if (!res.body) {
    throw new Error("SSE response has no body (browser may not support streaming)");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";

  // SSE 事件分隔符,按 spec 是任一: \r\n\r\n, \n\n, \r\r
  // sse-starlette 默认发 \r\n\r\n,所以必须先把 CRLF 归一化为 LF。
  const findBoundary = () => {
    const idx = buf.indexOf("\n\n");
    return idx >= 0 ? { idx, len: 2 } : null;
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // 关键: 把 \r\n 归一化为 \n,这样下面的 \n\n 切分对 sse-starlette 也生效
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

      let m;
      while ((m = findBoundary())) {
        const raw = buf.slice(0, m.idx);
        buf = buf.slice(m.idx + m.len);
        const evt = parseSSEBlock(raw);
        if (evt) yield evt;
      }
    }
    if (buf.trim()) {
      const evt = parseSSEBlock(buf.replace(/\r\n/g, "\n"));
      if (evt) yield evt;
    }
  } finally {
    try { reader.cancel(); } catch (_) {}
  }
}

function parseSSEBlock(raw) {
  if (!raw) return null;
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) continue;             // comment
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  const data = dataLines.join("\n");
  let parsed = data;
  try { parsed = JSON.parse(data); } catch (_) { /* keep string */ }
  return { event, data: parsed };
}

// ============================================================
// SREwise endpoint helpers
// ============================================================

export const api = {
  // 通用
  health: () => getJSON("/health"),

  // SRE
  diagnose: (body) => sseStream("/api/sre/diagnose", { method: "POST", body }),
  approve:  (body) => sseStream("/api/sre/approve",  { method: "POST", body }),
  pendingList: () => getJSON("/api/sre/pending"),
  pendingGet: (sid) => getJSON(`/api/sre/pending/${encodeURIComponent(sid)}`),

  // SRE 历史档案
  historyList: (params) => getJSON("/api/sre/history", params),
  historyGet: (sid) => getJSON(`/api/sre/history/${encodeURIComponent(sid)}`),
  historyReportUrl: (sid) =>
    `/api/sre/history/${encodeURIComponent(sid)}/report.md`,

  // KG
  kgStats: () => getJSON("/api/sre/kg/stats"),
  kgRootCauses: () => getJSON("/api/sre/kg/root-causes"),
  kgSimilar: (params) => getJSON("/api/sre/kg/similar", params),
  kgActions: (params) => getJSON("/api/sre/kg/actions", params),
  kgSubgraph: (params) => getJSON("/api/sre/kg/subgraph", params),

  // GraphRAG
  graphragQuery: (params) => getJSON("/api/sre/graphrag/query", params),
  graphragReseed: () => postJSON("/api/sre/graphrag/reseed", {}),

  // Eval
  evalScenarios: () => getJSON("/api/eval/scenarios"),
  evalRun: (body) => sseStream("/api/eval/run", { method: "POST", body }),
  evalLast: () => getJSON("/api/eval/last"),
};
