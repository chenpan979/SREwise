/**
 * GraphRAG Playground — 调试三路混合召回。
 *
 * - 左:Query 输入 + 各路 top_k 控制
 * - 中:三路结果分栏 (KG incidents / KG actions / Vector / Cross-seed)
 * - 右:命中元信息 + 性能指标
 */

import { api } from "../api.js";
import { h, badge, empty, loading, svgIcon, toast, escapeHtml, jsonBlock } from "../ui.js";

export default async function graphragPage() {
  const root = h("div", { class: "page" });

  root.appendChild(h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, "GraphRAG 调试台"),
      h("p", {}, "三路混合召回:Knowledge Graph 结构化 + Milvus 语义向量 + Cross-Seed 长尾"),
    ),
    h("div", { class: "page-actions" },
      h("button", { class: "btn", onClick: async () => {
        try {
          const r = await api.graphragReseed();
          toast("Runbook 已重灌", `written=${r.written} skipped=${r.skipped}`, "success");
        } catch (e) { toast("重灌失败", String(e), "error"); }
      } }, svgIcon("refresh"), "重灌 Runbook 种子"),
    ),
  ));

  const grid = h("div", { class: "page-body",
    style: { display: "grid", gridTemplateColumns: "320px 1fr", gap: "16px" } });
  root.appendChild(grid);

  // 左:Query 表单
  const qCard = h("div", { class: "card fill" });
  qCard.appendChild(h("div", { class: "card-head" }, h("h3", {}, "查询参数")));
  const qBody = h("div", { class: "card-body" });
  qCard.appendChild(qBody);
  grid.appendChild(qCard);

  const qInput = h("textarea", { class: "textarea", rows: 4,
    placeholder: "data-sync-service OOMKilled 反复重启" });
  qInput.value = "data-sync-service OOMKilled 反复重启";
  const svcInput = h("input", { class: "input", placeholder: "(任意)" });
  const rcSelect = h("select", { class: "select" }, h("option", { value: "" }, "(任意)"));
  const kwInput = h("input", { class: "input", placeholder: "OOMKilled,CrashLoopBackOff" });
  const topKgInput = h("input", { class: "input", value: "5", type: "number" });
  const topVecInput = h("input", { class: "input", value: "4", type: "number" });
  const topCrossInput = h("input", { class: "input", value: "3", type: "number" });
  const enableCross = h("input", { type: "checkbox", checked: true });

  qBody.appendChild(h("div", { class: "field" },
    h("label", {}, "Query (必填)"), qInput));
  qBody.appendChild(h("div", { class: "field" },
    h("label", {}, "Service hint"), svcInput));
  qBody.appendChild(h("div", { class: "field" },
    h("label", {}, "Root Cause"), rcSelect));
  qBody.appendChild(h("div", { class: "field" },
    h("label", {}, "症状关键字 (逗号)"), kwInput));
  qBody.appendChild(h("div", { class: "row gap-8" },
    h("div", { class: "field flex-1" }, h("label", {}, "top KG"), topKgInput),
    h("div", { class: "field flex-1" }, h("label", {}, "top Vec"), topVecInput),
    h("div", { class: "field flex-1" }, h("label", {}, "top Cross"), topCrossInput),
  ));
  qBody.appendChild(h("label", { class: "row gap-8 text-xs muted",
    style: { cursor: "pointer", marginBottom: "12px" } },
    enableCross, "启用 Cross-Seed (用 KG 命中作为新 query 二次召回)"));
  qBody.appendChild(h("button", { class: "btn btn-primary", style: { width: "100%" },
    onClick: doQuery }, svgIcon("search"), "查询"));

  // 加载根因下拉
  api.kgRootCauses().then((d) => {
    Object.entries(d?.categories || {}).forEach(([k, v]) => {
      rcSelect.appendChild(h("option", { value: k }, `${k} — ${v}`));
    });
  }).catch(() => {});

  // 右:结果 (独立滚动区域,不撑高页面)
  const resultsCol = h("div", { class: "col-fill",
    style: { overflowY: "auto", paddingRight: "4px" } });
  grid.appendChild(resultsCol);
  const resultsHost = h("div",
    { style: { display: "flex", flexDirection: "column", gap: "12px" } });
  resultsCol.appendChild(resultsHost);
  resultsHost.appendChild(emptyHint());

  async function doQuery() {
    const q = qInput.value.trim();
    if (!q) return toast("请输入 query", "", "warning");
    resultsHost.innerHTML = "";
    resultsHost.appendChild(loading("调用 GraphRAG..."));
    const t0 = performance.now();
    try {
      const params = {
        q, service: svcInput.value.trim() || undefined,
        root_cause: rcSelect.value || undefined,
        keywords: kwInput.value.trim() || undefined,
        top_k_kg: Number(topKgInput.value) || 5,
        top_k_vector: Number(topVecInput.value) || 4,
        top_k_cross: Number(topCrossInput.value) || 3,
        enable_cross_seed: enableCross.checked,
      };
      const data = await api.graphragQuery(params);
      const dt = performance.now() - t0;
      resultsHost.innerHTML = "";

      // 顶部 stat
      resultsHost.appendChild(h("div", { class: "stat-grid mb-12" },
        statBox("延迟", `${dt.toFixed(0)} ms`, "GraphRAG.query"),
        statBox("KG 故障", data.kg_incidents?.length || 0, "结构化召回"),
        statBox("KG 动作", data.kg_action_templates?.length || 0, "动作模板"),
        statBox("Vector chunks", data.vector_chunks?.length || 0, ""),
        statBox("Cross-seed", data.cross_seeded_chunks?.length || 0, ""),
      ));

      // KG 故障
      resultsHost.appendChild(sectionCard("KG · 相似故障实例",
        renderKgIncidents(data.kg_incidents || [])));
      // KG action templates
      resultsHost.appendChild(sectionCard("KG · 历史成功动作模板",
        renderActionTemplates(data.kg_action_templates || [])));
      // Vector chunks
      resultsHost.appendChild(sectionCard("向量 · 语义召回",
        renderChunks(data.vector_chunks || [])));
      // Cross-seed
      resultsHost.appendChild(sectionCard("Cross-Seed · 长尾召回",
        renderChunks(data.cross_seeded_chunks || [])));

      // raw json (debug)
      resultsHost.appendChild(sectionCard("原始响应 (JSON)",
        jsonBlock(data, 12000), { collapsed: true }));
    } catch (e) {
      resultsHost.innerHTML = "";
      resultsHost.appendChild(empty("查询失败", String(e), "alert"));
    }
  }

  return { node: root };
}

// ============================================================
function sectionCard(title, body, opts = {}) {
  const card = h("div", { class: "card" });
  let collapsed = !!opts.collapsed;
  const head = h("div", { class: "card-head", style: { cursor: "pointer" },
    onClick: () => {
      collapsed = !collapsed;
      bodyEl.style.display = collapsed ? "none" : "";
      arrow.textContent = collapsed ? "▸" : "▾";
    },
  },
    h("h3", {}, title),
    h("span", { class: "muted" }, "")
  );
  const arrow = h("span", { class: "muted mono", style: { marginRight: "8px" } },
    collapsed ? "▸" : "▾");
  head.appendChild(arrow);
  card.appendChild(head);
  const bodyEl = h("div", { class: "card-body tight",
    style: { display: collapsed ? "none" : "" } });
  if (body instanceof Node) bodyEl.appendChild(body);
  else if (typeof body === "string") bodyEl.innerHTML = body;
  card.appendChild(bodyEl);
  return card;
}

function statBox(label, value, meta) {
  return h("div", { class: "stat" },
    h("div", { class: "stat-label" }, label),
    h("div", { class: "stat-value" }, String(value)),
    meta && h("div", { class: "stat-meta" }, meta),
  );
}

function renderKgIncidents(items) {
  if (!items.length) return empty("无命中", "");
  const wrap = h("div");
  items.forEach((it) => {
    const inc = it.incident || {};
    const rc = it.root_cause || {};
    wrap.appendChild(h("div", { class: "wf-step", style: { margin: "0 12px 8px" } },
      h("div", { class: "wf-icon" }, svgIcon("alert", 14)),
      h("div", {},
        h("div", { class: "wf-stage" }, inc.alert_name || "Incident"),
        h("div", { class: "wf-time mono" }, inc.started_at || ""),
      ),
      h("div", {},
        h("div", { class: "wf-msg" }, `${inc.service || "?"} · ${rc.category || "unknown"}`),
        h("div", { class: "wf-detail" }, inc.summary || ""),
      ),
      badge(`score ${(it.score ?? 0).toFixed(1)}`, "purple"),
    ));
  });
  return wrap;
}

function renderActionTemplates(items) {
  if (!items.length) return empty("无 Action 模板", "");
  const wrap = h("div", { class: "action-list", style: { padding: "12px" } });
  items.forEach((t) => {
    wrap.appendChild(h("div", { class: "action-row" },
      h("div", { class: "meta" },
        h("div", { class: "tool" }, t.tool_name || "?"),
        t.args_signature && h("div", { class: "args" }, t.args_signature),
        h("div", { class: "rationale" },
          `成功 ${t.success_count} 次 · 命中 ${t.hit_count} 次`),
      ),
    ));
  });
  return wrap;
}

function renderChunks(chunks) {
  if (!chunks.length) return empty("无召回", "");
  const wrap = h("div", { style: { padding: "12px" } });
  chunks.forEach((c) => {
    const meta = c.metadata || {};
    wrap.appendChild(h("div", { class: "diag-card", style: { marginBottom: "8px" } },
      h("div", { class: "row gap-8 between" },
        h("span", { class: "badge purple" }, meta._kind || "chunk"),
        h("span", { class: "text-xs muted mono" },
          `${c.channel || "?"} · score ${(c.score ?? 0).toFixed(2)}`),
      ),
      h("div", { class: "row gap-8 text-xs muted mt-8" },
        meta.service && badge(meta.service, "info"),
        meta.root_cause && badge(meta.root_cause, "warning"),
        meta.severity && badge(meta.severity, severityKind(meta.severity)),
      ),
      h("div", { class: "mt-8 text-sm",
        style: { whiteSpace: "pre-wrap", color: "var(--fg-1)" } },
        (c.content || "").slice(0, 600)
        + ((c.content || "").length > 600 ? "..." : "")),
    ));
  });
  return wrap;
}

function severityKind(s) {
  return s === "critical" ? "danger" : s === "warning" ? "warning" : "info";
}

function emptyHint() {
  return h("div", { class: "empty" },
    svgIcon("search", 32),
    h("div", { class: "empty-title" }, "GraphRAG 混合召回调试台"),
    h("div", { class: "empty-sub" },
      "在左侧填写 query 和过滤条件,点击查询查看三路混合召回结果"),
  );
}
