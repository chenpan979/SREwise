/**
 * KG Explorer — 浏览 Neo4j Incident Knowledge Graph。
 *
 * 模块
 * ====
 * - 顶部: 节点 / 关系数,根因受控字典
 * - 左:  搜索面板 (service / root_cause / keywords)
 * - 中:  子图 SVG 可视化 (按 label 分簇布局,可点选 incident)
 * - 右:  搜索结果 + 选中节点详情 + Action 模板
 */

import { api } from "../api.js";
import {
  h, badge, empty, loading, svgIcon, toast, jsonBlock, escapeHtml,
} from "../ui.js";

export default async function kgPage() {
  const root = h("div", { class: "page" });

  root.appendChild(h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, "故障知识图谱"),
      h("p", {}, "(Incident)-[:CAUSED_BY]→(RootCause), (Incident)-[:RESOLVED_BY]→(Action), (Incident)-[:AFFECTED]→(Service) ..."),
    ),
    h("div", { class: "page-actions" },
      h("button", { class: "btn", onClick: () => refreshAll() },
        svgIcon("refresh"), "刷新"),
    ),
  ));

  // 顶部统计
  const statsRow = h("div", { class: "stat-grid" });
  root.appendChild(statsRow);

  // 三栏 (撑满剩余高度,各列自滚动)
  const grid = h("div", { class: "page-body",
    style: { display: "grid", gridTemplateColumns: "300px 1fr 360px", gap: "16px" } });
  root.appendChild(grid);

  // 左:搜索面板
  const searchCard = h("div", { class: "card fill" });
  searchCard.appendChild(h("div", { class: "card-head" }, h("h3", {}, "搜索故障实例")));
  const searchBody = h("div", { class: "card-body" });
  searchCard.appendChild(searchBody);
  grid.appendChild(searchCard);

  const serviceInput = h("input", { class: "input", placeholder: "service (e.g. data-sync-service)" });
  const rcSelect = h("select", { class: "select" }, h("option", { value: "" }, "(任意)"));
  const kwInput = h("input", { class: "input", placeholder: "OOMKilled,CrashLoop" });
  const limitInput = h("input", { class: "input", value: "8", type: "number", min: "1", max: "30" });

  searchBody.appendChild(h("div", { class: "field" },
    h("label", {}, "Service"), serviceInput));
  searchBody.appendChild(h("div", { class: "field" },
    h("label", {}, "Root Cause"), rcSelect));
  searchBody.appendChild(h("div", { class: "field" },
    h("label", {}, "症状关键词 (逗号)"), kwInput));
  searchBody.appendChild(h("div", { class: "field" },
    h("label", {}, "Limit"), limitInput));

  const searchBtn = h("button", { class: "btn btn-primary", style: { width: "100%" },
    onClick: doSearch }, svgIcon("search"), "搜索相似故障");
  searchBody.appendChild(searchBtn);

  // 中:Subgraph
  const graphCard = h("div", { class: "card fill" });
  const graphCenterBadge = h("span", { class: "badge" }, "全图");
  // 缩放按钮 — 拿到当前 svg (graphHost.querySelector('svg')) 调用其 zoom* 方法
  const zoomBtn = (icon, label, fn) =>
    h("button", { class: "btn btn-sm btn-ghost", title: label,
      onClick: () => {
        const svg = graphHost.querySelector("svg");
        if (svg && svg[fn]) svg[fn]();
      } }, svgIcon(icon, 14));
  graphCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "子图可视化"),
    h("div", { class: "row gap-8 align-center" },
      graphCenterBadge,
      h("div", { class: "zoom-controls row" },
        zoomBtn("search", "放大 (滚轮↑)", "zoomIn"),
        zoomBtn("inbox", "缩小 (滚轮↓)", "zoomOut"),
        zoomBtn("refresh", "重置视图", "zoomReset"),
      ),
      h("button", { class: "btn btn-sm btn-ghost",
        onClick: () => loadSubgraph(null) }, "查看全图"),
    ),
  ));
  const graphHost = h("div", { class: "graph-canvas" });
  graphCard.appendChild(graphHost);
  graphHost.appendChild(emptyGraphHint());
  grid.appendChild(graphCard);
  // 浮层提示在 SVG 加载完成后重新挂(每次 loadSubgraph 会清空 graphHost)
  function attachGraphHint() {
    graphHost.appendChild(h("div", { class: "graph-hint" },
      "滚轮缩放 · 拖拽平移 · 点节点查看详情"));
  }

  // 右:结果 / 详情
  const rightCol = h("div", { class: "col-fill" });
  grid.appendChild(rightCol);

  const resultsCard = h("div", { class: "card fill" });
  resultsCard.appendChild(h("div", { class: "card-head" }, h("h3", {}, "搜索结果")));
  const resultsBody = h("div", { class: "card-body tight" });
  resultsCard.appendChild(resultsBody);
  resultsBody.appendChild(empty("尚未搜索", "在左侧填写过滤条件后点击搜索"));
  rightCol.appendChild(resultsCard);

  const detailCard = h("div", { class: "card fill" });
  detailCard.appendChild(h("div", { class: "card-head" }, h("h3", {}, "节点详情")));
  const detailBody = h("div", { class: "card-body" });
  detailCard.appendChild(detailBody);
  detailBody.appendChild(empty("点击节点", "在子图或结果列表中选择"));
  rightCol.appendChild(detailCard);

  // 加载根因字典 + 统计 (节点已挂在 root 上,但还未挂到 document)
  refreshAll();

  async function refreshAll() {
    statsRow.innerHTML = "";
    statsRow.appendChild(h("div", { class: "stat" },
      h("div", { class: "stat-label" }, "状态"),
      h("div", { class: "stat-value" }, h("span", { class: "spinner" })),
    ));
    try {
      const [stats, rcDict] = await Promise.all([api.kgStats(), api.kgRootCauses()]);
      statsRow.innerHTML = "";
      if (!stats.ready) {
        statsRow.appendChild(h("div", { class: "stat" },
          h("div", { class: "stat-label" }, "Neo4j"),
          h("div", { class: "stat-value" }, "Not Ready"),
          h("div", { class: "stat-meta" }, "请配置 .env 并重启"),
        ));
        return;
      }
      const k = stats.nodes_by_kind || {};
      // 第二列起的 kind 必须与 .gnode.<Label> CSS 一致,这样色点跟子图圈同色
      [
        ["节点总数", stats.node_count || 0, `${stats.edge_count || 0} 条关系`, ""],
        ["故障实例", k.Incident || 0, "Incident", "Incident"],
        ["服务", k.Service || 0, "Service", "Service"],
        ["根因类别", k.RootCause || 0, "RootCause", "RootCause"],
        ["动作模板", k.Action || 0, "Action", "Action"],
      ].forEach(([l, v, s, kind]) =>
        statsRow.appendChild(makeStat(l, v, s, kind)));

      // 填充根因下拉
      rcSelect.innerHTML = "";
      rcSelect.appendChild(h("option", { value: "" }, "(任意)"));
      Object.entries(rcDict?.categories || {}).forEach(([key, desc]) => {
        rcSelect.appendChild(h("option", { value: key }, `${key} — ${desc}`));
      });

      // 默认加载全图
      loadSubgraph(null);
    } catch (e) {
      statsRow.innerHTML = "";
      statsRow.appendChild(h("div", { class: "stat" },
        h("div", { class: "stat-label" }, "错误"),
        h("div", { class: "stat-value", style: { color: "var(--red)" } }, "加载失败"),
        h("div", { class: "stat-meta" }, String(e)),
      ));
    }
  }

  // ---------------- 搜索 ----------------
  async function doSearch() {
    resultsBody.innerHTML = "";
    resultsBody.appendChild(loading("查询 Neo4j..."));
    try {
      const params = {
        service: serviceInput.value.trim() || undefined,
        root_cause: rcSelect.value || undefined,
        keywords: kwInput.value.trim() || undefined,
        limit: Number(limitInput.value) || 8,
      };
      const res = await api.kgSimilar(params);
      resultsBody.innerHTML = "";
      const items = res?.items || [];
      if (!items.length) {
        resultsBody.appendChild(empty("无匹配 incident",
          "试着换关键词或放开 service 过滤"));
        return;
      }
      const list = h("div");
      items.forEach((it) => {
        const inc = it.incident || {};
        const rc = it.root_cause || {};
        const acts = it.resolved_actions || [];
        const score = it.score ?? 0;
        const row = h("div", { class: "wf-step",
          style: { cursor: "pointer", margin: "0 12px 8px" },
          onClick: () => {
            loadSubgraph(inc.id);
            renderIncidentDetail(it);
          },
        },
          h("div", { class: "wf-icon" }, svgIcon("alert", 16)),
          h("div", {},
            h("div", { class: "wf-stage" }, inc.alert_name || "Incident"),
            h("div", { class: "wf-time mono" }, inc.started_at || ""),
          ),
          h("div", {},
            h("div", { class: "wf-msg" }, `${inc.service || "?"} · ${rc.category || "unknown"}`),
            h("div", { class: "wf-detail" }, `${acts.length} actions · score ${score.toFixed(1)}`),
          ),
          badge(inc.severity || "info", severityKind(inc.severity)),
        );
        list.appendChild(row);
      });
      resultsBody.appendChild(list);

      // Also load action templates for the top hit's root_cause if available
      if (items[0]?.root_cause?.category) {
        loadActionTemplates(items[0].root_cause.category, items[0].incident?.service);
      }
    } catch (e) {
      resultsBody.innerHTML = "";
      resultsBody.appendChild(empty("搜索失败", String(e), "alert"));
    }
  }

  function renderIncidentDetail(it) {
    detailBody.innerHTML = "";
    const inc = it.incident || {};
    const rc = it.root_cause || {};
    const acts = it.resolved_actions || [];
    detailBody.appendChild(h("div", { class: "diag-card" },
      h("div", { class: "text-xs muted strong" }, "故障实例"),
      h("div", { class: "diag-rc mt-8" }, inc.alert_name || "(未命名)"),
      h("div", { class: "row between text-xs muted mt-12" },
        h("span", {}, inc.service || "?"),
        h("span", {}, inc.started_at || ""),
      ),
      h("div", { class: "text-xs mt-12" }, inc.summary || ""),
    ));
    detailBody.appendChild(h("div", { class: "diag-card mt-12" },
      h("div", { class: "text-xs muted strong" }, "根因类别"),
      h("div", { class: "diag-rc mt-8" }, rc.category || "?"),
      h("div", { class: "text-xs muted mt-8" }, rc.description || ""),
    ));
    if (acts.length) {
      detailBody.appendChild(h("div", { class: "diag-card mt-12" },
        h("div", { class: "text-xs muted strong" }, `历史修复动作 (${acts.length})`),
        h("div", { class: "action-list" },
          ...acts.map((a) =>
            h("div", { class: "action-row" },
              h("div", { class: "meta" },
                h("div", { class: "tool" }, a.tool_name || "?"),
                a.args_signature && h("div", { class: "args" }, a.args_signature),
              ),
              badge(a.success === false ? "fail" : "ok",
                a.success === false ? "danger" : "success"),
            )
          ),
        ),
      ));
    }
  }

  async function loadActionTemplates(rcCategory, service) {
    try {
      const data = await api.kgActions({ root_cause: rcCategory, service, limit: 5 });
      const items = data?.items || [];
      if (!items.length) return;
      const block = h("div", { class: "diag-card mt-12" },
        h("div", { class: "text-xs muted strong" },
          `动作模板 · ${rcCategory}`),
        h("div", { class: "action-list" },
          ...items.map((t) =>
            h("div", { class: "action-row" },
              h("div", { class: "meta" },
                h("div", { class: "tool" }, t.tool_name || "?"),
                t.args_signature && h("div", { class: "args" }, t.args_signature),
                h("div", { class: "rationale" },
                  `成功 ${t.success_count} 次 · 命中 ${t.hit_count} 次`),
              ),
            )
          ),
        ),
      );
      detailBody.appendChild(block);
    } catch (_) { /* 静默 */ }
  }

  // ---------------- Subgraph ----------------
  async function loadSubgraph(centerId) {
    graphHost.innerHTML = "";
    graphHost.appendChild(loading(centerId ? `加载子图 ${centerId} ...` : "加载全图..."));
    graphCenterBadge.textContent =
      centerId ? `中心: ${centerId.slice(0, 16)}…` : "全图";
    try {
      const data = await api.kgSubgraph({
        incident_id: centerId || undefined,
        depth: 2, limit_nodes: centerId ? 60 : 100,
      });
      graphHost.innerHTML = "";
      if (!data?.nodes?.length) {
        graphHost.appendChild(empty("KG 为空",
          "等待 Reporter 写入第一条诊断,或运行 GraphRAG reseed", "graph"));
        return;
      }
      graphHost.appendChild(renderGraphSVG(data, (node) => {
        renderNodeDetail(node);
        if (node.labels?.includes("Incident")) loadSubgraph(node.props?.id);
      }));
      attachGraphHint();
    } catch (e) {
      graphHost.innerHTML = "";
      graphHost.appendChild(empty("子图加载失败", String(e), "alert"));
    }
  }

  function renderNodeDetail(node) {
    detailBody.innerHTML = "";
    const labels = (node.labels || []).join(", ");
    detailBody.appendChild(h("div", { class: "diag-card" },
      h("div", { class: "text-xs muted strong" }, labels),
      h("div", { class: "diag-rc mt-8" },
        node.props?.id || node.props?.alert_name || node.props?.name || node.id),
    ));
    detailBody.appendChild(jsonBlock(node.props || {}));
  }

  return { node: root };
}

// ============================================================
// 子图 SVG 渲染:按 label 分簇,环形布局
// ============================================================
function renderGraphSVG(data, onNodeClick) {
  const W = 720, H = 540, CX = W / 2, CY = H / 2;
  const nodes = data.nodes || [];
  const edges = data.edges || [];

  // 按 label 分组
  const groups = {};
  nodes.forEach((n) => {
    const lab = (n.labels || ["Other"])[0];
    if (!groups[lab]) groups[lab] = [];
    groups[lab].push(n);
  });

  // 把组分配到不同同心环 / 角度
  const groupKeys = Object.keys(groups);
  const positions = new Map();   // node.id -> {x, y}

  // Incident 在中心,其他在外圈
  const ringR = Math.min(W, H) * 0.36;
  const innerR = ringR * 0.42;

  // 中心环: Incident
  const centerNodes = groups["Incident"] || [];
  centerNodes.forEach((n, i) => {
    const a = (i / Math.max(1, centerNodes.length)) * 2 * Math.PI;
    positions.set(n.id, { x: CX + Math.cos(a) * innerR, y: CY + Math.sin(a) * innerR });
  });

  // 外圈: 其他 label
  const otherKeys = groupKeys.filter((k) => k !== "Incident");
  otherKeys.forEach((key, gi) => {
    const baseAngle = (gi / Math.max(1, otherKeys.length)) * 2 * Math.PI;
    const arr = groups[key];
    arr.forEach((n, i) => {
      const spread = Math.PI / 4;
      const a = baseAngle - spread / 2 + (i / Math.max(1, arr.length - 1 || 1)) * spread;
      positions.set(n.id, { x: CX + Math.cos(a) * ringR, y: CY + Math.sin(a) * ringR });
    });
  });

  // SVG
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  // 缩放/平移层: 所有 edges + nodes 都挂在这层上, 通过 transform 实现 zoom & pan
  const gZoom = document.createElementNS(NS, "g");
  gZoom.setAttribute("class", "zoom-layer");
  svg.appendChild(gZoom);

  // edges
  const gEdges = document.createElementNS(NS, "g");
  edges.forEach((e) => {
    const a = positions.get(e.start), b = positions.get(e.end);
    if (!a || !b) return;
    const path = document.createElementNS(NS, "line");
    path.setAttribute("x1", a.x); path.setAttribute("y1", a.y);
    path.setAttribute("x2", b.x); path.setAttribute("y2", b.y);
    path.setAttribute("class", "glink");
    gEdges.appendChild(path);
    // label (rel type) at midpoint
    if (e.type) {
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", (a.x + b.x) / 2);
      t.setAttribute("y", (a.y + b.y) / 2);
      t.setAttribute("class", "glink-label");
      t.setAttribute("text-anchor", "middle");
      t.textContent = e.type;
      gEdges.appendChild(t);
    }
  });
  gZoom.appendChild(gEdges);

  // nodes
  const gNodes = document.createElementNS(NS, "g");
  nodes.forEach((n) => {
    const p = positions.get(n.id); if (!p) return;
    const lab = (n.labels || ["Other"])[0];
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", `gnode ${lab}`);
    g.setAttribute("transform", `translate(${p.x}, ${p.y})`);

    const c = document.createElementNS(NS, "circle");
    c.setAttribute("r", lab === "Incident" ? 10 : 8);
    g.appendChild(c);

    const label = document.createElementNS(NS, "text");
    label.setAttribute("y", -14);
    label.setAttribute("text-anchor", "middle");
    label.textContent = nodeLabel(n);
    g.appendChild(label);

    g.addEventListener("click", () => onNodeClick && onNodeClick(n));
    gNodes.appendChild(g);
  });
  gZoom.appendChild(gNodes);

  // ---------------- 缩放 + 平移 ----------------
  // 状态: scale, tx, ty (世界坐标系内的 transform)
  let scale = 1, tx = 0, ty = 0;
  const MIN = 0.4, MAX = 4;
  function apply() {
    gZoom.setAttribute("transform", `translate(${tx} ${ty}) scale(${scale})`);
  }
  function setScale(next, cx = W / 2, cy = H / 2) {
    next = Math.min(MAX, Math.max(MIN, next));
    if (next === scale) return;
    // 让缩放围绕 (cx, cy) 进行: 保持该点世界坐标不变
    const k = next / scale;
    tx = cx - k * (cx - tx);
    ty = cy - k * (cy - ty);
    scale = next;
    apply();
  }
  // 滚轮缩放 (光标处为锚点)
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    // 把屏幕坐标换算成 viewBox 坐标
    const cx = ((e.clientX - rect.left) / rect.width) * W;
    const cy = ((e.clientY - rect.top) / rect.height) * H;
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    setScale(scale * factor, cx, cy);
  }, { passive: false });
  // 鼠标拖动平移
  let dragging = false, lastX = 0, lastY = 0;
  svg.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".gnode")) return;   // 点节点不开始拖
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    svg.setPointerCapture(e.pointerId);
    svg.style.cursor = "grabbing";
  });
  svg.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const rect = svg.getBoundingClientRect();
    const dx = ((e.clientX - lastX) / rect.width) * W;
    const dy = ((e.clientY - lastY) / rect.height) * H;
    tx += dx; ty += dy;
    lastX = e.clientX; lastY = e.clientY;
    apply();
  });
  const stopDrag = (e) => {
    dragging = false;
    try { svg.releasePointerCapture(e.pointerId); } catch (_) {}
    svg.style.cursor = "";
  };
  svg.addEventListener("pointerup", stopDrag);
  svg.addEventListener("pointercancel", stopDrag);

  // 暴露给外部按钮
  svg.zoomIn = () => setScale(scale * 1.25);
  svg.zoomOut = () => setScale(scale / 1.25);
  svg.zoomReset = () => { scale = 1; tx = 0; ty = 0; apply(); };
  svg.getScale = () => scale;

  return svg;
}

function nodeLabel(n) {
  const p = n.props || {};
  return (p.alert_name || p.category || p.name || p.tool_name || p.pattern || (p.id?.slice?.(0, 8)) || "?")
    .toString().slice(0, 24);
}

function emptyGraphHint() {
  const div = document.createElement("div");
  div.className = "empty";
  div.innerHTML = `<div class="empty-title">点击左侧搜索或顶部"查看全图"</div>
    <div class="empty-sub">节点颜色:故障=红 · 服务=蓝 · 根因=橙 · 动作=绿 · 症状=紫</div>`;
  return div;
}

function severityKind(sev) {
  return sev === "critical" ? "danger" : sev === "warning" ? "warning" : "info";
}

function makeStat(label, value, meta, kind) {
  // kind 与 .gnode.<Label> 同名 (Incident / Service / RootCause / Action / Symptom)
  // 这样 stat-dot 颜色跟子图圈完全一致,用户一眼对应。
  const root = document.createElement("div"); root.className = "stat";
  const dot = kind
    ? `<span class="stat-dot kind-${escapeHtml(kind)}" title="${escapeHtml(kind)}"></span>`
    : "";
  root.innerHTML = `
    <div class="stat-label">${dot}${escapeHtml(label)}</div>
    <div class="stat-value">${escapeHtml(String(value))}</div>
    <div class="stat-meta">${escapeHtml(meta || "")}</div>`;
  return root;
}
