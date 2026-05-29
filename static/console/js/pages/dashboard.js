/**
 * Dashboard — 系统总览。
 *
 * 模块:
 * 1. 健康面板 (Milvus/Neo4j/Langfuse 状态)
 * 2. KG 统计卡片 (Incident/Service/Action/RootCause/Symptom 节点数)
 * 3. 待审批列表
 * 4. 最近一次 Eval 摘要
 * 5. 快速操作 (跳转 + 触发 Eval / 重灌 GraphRAG)
 */

import { api } from "../api.js";
import { h, badge, empty, loading, svgIcon, toast, fmtTime, pct } from "../ui.js";
import { router } from "../router.js";
import { store } from "../store.js";

export default async function dashboardPage() {
  const root = h("div", { class: "page" });

  // Header
  root.appendChild(h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, "系统总览"),
      h("p", {}, "SREwise 多 Agent SRE 系统的实时运行状态"),
    ),
    h("div", { class: "page-actions" },
      h("button", { class: "btn", onClick: () => location.reload() },
        svgIcon("refresh"), "刷新页面"),
      h("button", { class: "btn btn-primary", onClick: () => router.navigate("incidents") },
        svgIcon("bolt"), "发起诊断"),
    ),
  ));

  // 健康卡片(立即从 store 拿,后台异步刷新会通过 store 通知)
  const healthRow = h("div", { class: "stat-grid" });
  root.appendChild(healthRow);

  // 主区域: KG 统计卡 + Pending + Eval (三栏,占满剩余高度)
  const grid = h("div", { class: "page-body",
    style: { display: "grid", gridTemplateColumns: "1.4fr 1fr 1fr", gap: "16px" } });
  const kgCard = h("div", { class: "card fill" });
  const pendingCard = h("div", { class: "card fill" });
  const evalCard = h("div", { class: "card fill" });
  grid.append(kgCard, pendingCard, evalCard);
  root.appendChild(grid);

  // ---------- 渲染健康行 ----------
  const renderHealth = () => {
    const data = store.get("health")?.data || {};
    healthRow.innerHTML = "";
    healthRow.appendChild(stat("整体状态", topLevelStatus(data),
      data?.error ? data.error : `服务:${data?.service || "-"}`,
      statTone(data?.status)));
    healthRow.appendChild(stat("Milvus 向量库",
      cnStatus(data?.milvus?.status), data?.milvus?.message || "",
      statTone(data?.milvus?.status)));
    healthRow.appendChild(stat("Neo4j 知识图谱",
      cnStatus(data?.incident_kg?.status), data?.incident_kg?.message || "",
      statTone(data?.incident_kg?.status)));
    healthRow.appendChild(stat("Langfuse 可观测性",
      cnStatus(data?.langfuse?.status), data?.langfuse?.message || "",
      statTone(data?.langfuse?.status)));
  };
  renderHealth();
  const offHealth = store.on("health", renderHealth);

  // ---------- KG 统计卡 ----------
  kgCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "故障知识图谱"),
    h("a", { class: "btn btn-sm btn-ghost", href: "#/kg" }, "打开", svgIcon("external")),
  ));
  const kgBody = h("div", { class: "card-body" }, loading("查询 KG 统计..."));
  kgCard.appendChild(kgBody);
  api.kgStats().then((data) => {
    kgBody.innerHTML = "";
    if (!data?.ready) {
      kgBody.appendChild(empty("Neo4j 未就绪", "请在 .env 配置 NEO4J_URI / NEO4J_PASSWORD,然后重启"));
      return;
    }
    const counts = data?.nodes_by_kind || {};
    const total = data?.node_count ?? Object.values(counts).reduce((a, b) => a + (b || 0), 0);
    kgBody.appendChild(h("div", { class: "stat-grid" },
      stat("总节点数", String(total), `${data?.edge_count ?? 0} 条关系`),
      stat("故障实例", String(counts.Incident || 0), "Incident"),
      stat("服务", String(counts.Service || 0), "Service"),
      stat("根因类别", String(counts.RootCause || 0), "RootCause"),
      stat("动作模板", String(counts.Action || 0), "Action"),
      stat("症状指纹", String(counts.Symptom || 0), "Symptom"),
    ));
    if (data.seeded) {
      kgBody.appendChild(h("div", { class: "mt-12 text-xs muted" },
        "✅ KG 已 seeded · ", h("span", { class: "mono" }, fmtTime())));
    }
  }).catch((e) => {
    kgBody.innerHTML = "";
    kgBody.appendChild(empty("KG 查询失败", String(e), "alert"));
  });

  // ---------- 人工处置记录卡 (审计 / 溯源 优先,待审批入口下沉) ----------
  // 设计目标
  //  - 不再当"审批入口" (审批入口在故障诊断页,这里只做审计)
  //  - 列表展示最近 N 条已处理的诊断: 谁(reviewer) / 决策 / 动作数 / 时间 / service
  //  - 顶部一行 hint: 若仍有 pending,提示"另有 N 个等待审批 → 故障诊断页处理"
  //  - 点击行 = 跳"故障档案"页查看完整详情
  pendingCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "人工处置记录"),
    h("a", { class: "btn btn-sm btn-ghost", href: "#/history" },
      svgIcon("external"), "查看档案"),
  ));
  const pendingHint = h("div", { class: "audit-hint", hidden: true });
  pendingCard.appendChild(pendingHint);
  const auditBody = h("div", { class: "card-body tight" }, loading("加载..."));
  pendingCard.appendChild(auditBody);

  function renderPendingHint(items) {
    const list = items || store.get("pending.items") || [];
    if (!list.length) {
      pendingHint.hidden = true;
      pendingHint.innerHTML = "";
      return;
    }
    pendingHint.hidden = false;
    pendingHint.innerHTML = "";
    pendingHint.append(
      svgIcon("alert", 14),
      h("span", {},
        h("strong", {}, `${list.length} 个 session 等待人工审批`),
        " · "),
      h("a", { href: "#/incidents" }, "前往故障诊断页处理 →"),
    );
  }

  // 决策标签 → 颜色
  function decisionBadge(d) {
    const m = {
      approved: ["全部批准", "success"],
      partial:  ["部分批准", "warning"],
      rejected: ["全部拒绝", "danger"],
      no_actions: ["无需动作", ""],
    };
    const [label, tone] = m[d] || [d || "?", ""];
    return badge(label, tone);
  }

  function renderAudit(items) {
    auditBody.innerHTML = "";
    const list = items || [];
    if (!list.length) {
      auditBody.appendChild(empty("暂无处置记录",
        "执行过人工审批的诊断会自动归档于此,可点上方「查看档案」追溯全部"));
      return;
    }
    const wrap = h("div", { class: "audit-list" });
    list.slice(0, 8).forEach((it) => {
      const sid = it.session_id;
      const reviewers = it.reviewers || [];
      const reviewer = reviewers.length
        ? reviewers.join(", ") : "—";
      const isError = it.status !== "completed";
      const summary = (it.root_cause || it.alert_name
        || it.service || "(无摘要)").toString();

      const row = h("a", {
        class: "audit-row",
        href: `#/history?session_id=${encodeURIComponent(sid)}`,
      },
        // 第一行: 决策 + 服务 + 时间
        h("div", { class: "row align-center gap-8 wrap" },
          decisionBadge(it.decision),
          isError && badge("异常", "danger"),
          it.service && h("span", { class: "badge" },
            Array.isArray(it.service) ? it.service[0] : it.service),
          h("span", { class: "audit-time mono text-xs muted" },
            it.finished_at || ""),
        ),
        // 第二行: 根因摘要
        h("div", { class: "audit-rc" }, summary),
        // 第三行: 谁处理 + 动作统计 + session_id
        h("div", { class: "row align-center gap-8 wrap text-xs muted mt-6" },
          h("span", {},
            svgIcon("shield", 12),
            " 处理人: ",
            h("span", { class: "strong" }, reviewer)),
          h("span", {},
            `批准 ${it.approved_count || 0}/${it.proposed_count || 0}`),
          h("span", {},
            `执行 ${it.executed_ok || 0}/${it.executed_total || 0}`),
          h("span", { class: "mono" },
            sid.slice(0, 22) + (sid.length > 22 ? "…" : "")),
        ),
        // 第四行 (可选): 备注
        (it.review_comments || []).length > 0
          && h("div", { class: "audit-comment text-xs muted mt-6" },
            "备注: ", (it.review_comments || []).join(" · ")),
      );
      wrap.appendChild(row);
    });
    auditBody.appendChild(wrap);
  }

  // 拉历史 (只拿最近 8 条) + 同步 pending hint
  function refreshAudit() {
    api.historyList({ limit: 8 })
      .then((d) => renderAudit(d?.items || []))
      .catch(() => {
        auditBody.innerHTML = "";
        auditBody.appendChild(empty("加载失败", "无法获取处置记录", "alert"));
      });
  }
  refreshAudit();
  renderPendingHint(store.get("pending.items"));
  // pending 列表变化 (轮询) 时刷新 hint, 同时刷新 audit (因为新批准会进 history)
  const offPending = store.on("pending.items", (items) => {
    renderPendingHint(items);
    refreshAudit();
  });

  // ---------- Eval 卡 ----------
  evalCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "最近一次评测"),
    h("a", { class: "btn btn-sm btn-ghost", href: "#/eval" }, "评测中心", svgIcon("external")),
  ));
  const evalBody = h("div", { class: "card-body" }, loading("查询..."));
  evalCard.appendChild(evalBody);
  api.evalLast().then((data) => {
    evalBody.innerHTML = "";
    if (!data?.result) {
      evalBody.appendChild(empty("未跑过 Eval",
        "在 Eval 页触发或 CLI 执行 python -m app.eval"));
      return;
    }
    const r = data.result;
    evalBody.appendChild(h("div", { class: "stat-grid",
      style: { gridTemplateColumns: "1fr 1fr" } },
      stat("通过率", pct(r.pass_rate), `${r.passed}/${r.total} 个场景`),
      stat("根因命中", pct(r.rc_hit_rate), "诊断匹配期望"),
      stat("修复召回", pct(r.action_hit_rate), "动作命中期望"),
      stat("安全门违规", String(r.safety_violations_total),
        r.safety_violations_total === 0 ? "✅ 全部通过" : "⚠️ 有违规"),
    ));
  }).catch(() => {
    evalBody.innerHTML = "";
    evalBody.appendChild(empty("Eval 状态获取失败", "", "alert"));
  });

  // 清理订阅
  return {
    node: root,
    cleanup() { offHealth(); offPending(); },
  };
}

// ============================================================
function stat(label, value, meta, tone) {
  return h("div", { class: `stat ${tone ? "tone-" + tone : ""}` },
    h("div", { class: "stat-label" }, label),
    h("div", { class: "stat-value" }, String(value)),
    meta && h("div", { class: "stat-meta" }, meta),
  );
}

function cnStatus(s) {
  if (!s) return "未知";
  const map = { connected: "已连接", configured: "已配置", healthy: "正常",
    disabled: "未启用", disconnected: "已断开",
    not_configured: "未配置", error: "错误", degraded: "降级", down: "失联",
    unhealthy: "不可用" };
  return map[s] || s;
}

function statTone(s) {
  // ok | warn | bad | mute
  if (!s) return "mute";
  if (["connected", "healthy", "configured"].includes(s)) return "ok";
  if (["degraded"].includes(s)) return "warn";
  if (["disconnected", "down", "error", "unhealthy"].includes(s)) return "bad";
  return "mute";
}

function topLevelStatus(data) {
  if (!data) return "?";
  return { healthy: "正常", degraded: "降级", unhealthy: "不可用" }[data.status]
    || (data.status || "?");
}
