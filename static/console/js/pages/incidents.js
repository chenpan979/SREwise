/**
 * Incidents — 实时故障诊断 + HITL 审批。
 *
 * 三栏布局
 * ========
 * - 左:触发面板 (alert 表单 / 历史 session)
 * - 中:Agent 瀑布流 (SSE 实时事件)
 * - 右:Diagnosis & Actions 详情 + 复盘报告
 */

import { api } from "../api.js";
import {
  h, badge, riskBadge, severityBadge, empty, loading, svgIcon,
  toast, fmtTime, jsonBlock, renderMarkdown, copyText, showModal,
} from "../ui.js";
import { router } from "../router.js";
import { store } from "../store.js";

const STAGE_LABELS = {
  initializing: "初始化",
  supervisor: "Supervisor 路由",
  historian: "Historian 召回",
  diagnostician: "Diagnostician 诊断",
  remediator: "Remediator 提议",
  human_review: "人工审核",
  awaiting_approval: "等待审批",
  executor: "Executor 执行",
  reporter: "Reporter 复盘",
  diagnosis_complete: "诊断完成",
  resuming: "恢复执行",
  exception: "异常",
};

const QUICK_ALERTS = [
  {
    title: "OOM 标准剧本", icon: "alert",
    payload: {
      alert: {
        name: "PodCrashLooping", severity: "critical",
        service: "data-sync-service", namespace: "production",
        summary: "Pod data-sync-service-* 反复重启 (>5 次/15 分钟)",
        description: "production/data-sync-service 下 Pod 在过去 18 分钟内进入 CrashLoopBackOff,exit 137 (OOMKilled)。",
      },
    },
  },
  {
    title: "高内存预警(温和)", icon: "shield",
    payload: {
      alert: {
        name: "HighMemoryUsage", severity: "warning",
        service: "data-sync-service", namespace: "production",
        summary: "data-sync-service 内存使用率 94%",
        description: "container_memory_usage_bytes 持续 > 90% memory_limit",
      },
    },
  },
  {
    title: "用户主动健康巡检", icon: "search",
    payload: { query: "请对生产环境做一次健康巡检,看是否有潜在风险,不要执行任何变更动作。" },
  },
];

// ============================================================
export default async function incidentsPage(params) {
  const root = h("div", { class: "page" });
  let abortCtrl = null;       // 当前 SSE 控制
  let currentSession = params.session_id || `inc-${Date.now()}`;
  let currentInterruptPayload = null;  // {proposed_actions, diagnosis, ...}

  // Header
  root.appendChild(h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, "Incidents · 多 Agent 诊断"),
      h("p", {}, "Supervisor → Historian → Diagnostician → Remediator → HITL → Executor → Reporter"),
    ),
    h("div", { class: "page-actions" },
      h("div", { class: "badge mono" }, "session: " + currentSession),
      h("button", { class: "btn", onClick: () => copyText(currentSession) },
        svgIcon("copy"), "Copy"),
    ),
  ));

  // 三栏 layout (page-body 撑满剩余高度,各列内部滚动)
  const grid = h("div", { class: "page-body grid-3" });
  root.appendChild(grid);

  // ---------------- 左栏: 触发面板 + 待审批 ----------------
  const leftCol = h("div", { class: "col-fill" });
  grid.appendChild(leftCol);

  const triggerCard = h("div", { class: "card fill" });
  triggerCard.appendChild(h("div", { class: "card-head" }, h("h3", {}, "发起诊断")));
  const triggerBody = h("div", { class: "card-body" });
  triggerCard.appendChild(triggerBody);

  // 快捷剧本
  triggerBody.appendChild(h("div", { class: "text-xs muted mb-8" }, "快速剧本"));
  QUICK_ALERTS.forEach((p) => {
    const btn = h("button", { class: "btn mb-8",
      style: { width: "100%", justifyContent: "flex-start" },
      onClick: () => startDiagnose(p.payload) },
      svgIcon(p.icon), p.title);
    triggerBody.appendChild(btn);
  });

  triggerBody.appendChild(h("div", { class: "divider" }));

  // 自定义 alert (折叠)
  triggerBody.appendChild(h("div", { class: "text-xs muted mb-8" }, "自定义 query / alert"));
  const queryInput = h("textarea", {
    class: "textarea", placeholder: "输入诊断描述 (留空则用 alert 字段)",
    rows: 3,
  });
  triggerBody.appendChild(queryInput);
  triggerBody.appendChild(h("div", { class: "help mt-8" },
    "可选: 也可在下方 JSON 写 alert 对象"));
  const alertJson = h("textarea", {
    class: "textarea mt-8", placeholder: '{"name":"PodCrashLooping","severity":"critical",...}',
    rows: 5,
  });
  triggerBody.appendChild(alertJson);
  triggerBody.appendChild(h("button", {
    class: "btn btn-primary mt-12", style: { width: "100%" },
    onClick: () => {
      const q = queryInput.value.trim();
      let alert = null;
      if (alertJson.value.trim()) {
        try { alert = JSON.parse(alertJson.value); }
        catch (e) { return toast("Alert JSON 解析失败", String(e), "error"); }
      }
      if (!q && !alert) return toast("请填写 query 或 alert", "", "warning");
      startDiagnose({ query: q || null, alert });
    },
  }, svgIcon("play"), "运行诊断"));
  leftCol.appendChild(triggerCard);

  // 待审批列表
  const pendingCard = h("div", { class: "card fill" });
  pendingCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "待审批列表"),
    h("button", { class: "btn btn-sm btn-ghost", onClick: () => refreshPending() },
      svgIcon("refresh")),
  ));
  const pendingBody = h("div", { class: "card-body tight" });
  pendingCard.appendChild(pendingBody);
  leftCol.appendChild(pendingCard);

  async function refreshPending() {
    pendingBody.innerHTML = "";
    pendingBody.appendChild(loading("查询..."));
    try {
      const data = await api.pendingList();
      pendingBody.innerHTML = "";
      const items = data?.items || [];
      // 同步到全局 store, 让总览页/导航红点立即一致
      store.set("pending.items", items);
      store.set("pending.count", items.length);
      if (!items.length) {
        pendingBody.appendChild(empty("空", "暂无待审批 session"));
        return;
      }
      items.forEach((item) => {
        const row = h("div", {
          class: "wf-step interrupt",
          "data-session": item.session_id,
          style: { cursor: "pointer", margin: "0 12px 8px" },
          onClick: async () => {
            currentSession = item.session_id;
            try {
              const detail = await api.pendingGet(item.session_id);
              currentInterruptPayload = detail;
              renderInterruptPanel(detail);
            } catch (e) { toast("获取详情失败", String(e), "error"); }
          },
        },
          h("div", { class: "wf-icon" }, svgIcon("alert", 16)),
          h("div", {},
            h("div", { class: "wf-stage" }, "待审批"),
            h("div", { class: "wf-time mono" }, item.interrupted_at || ""),
          ),
          h("div", {},
            h("div", { class: "wf-msg truncate" }, item.session_id),
            h("div", { class: "wf-detail" },
              `${(item.proposed_actions || []).length} 个候选`),
          ),
        );
        pendingBody.appendChild(row);
      });
    } catch (e) {
      pendingBody.innerHTML = "";
      pendingBody.appendChild(empty("加载失败", String(e), "alert"));
    }
  }
  refreshPending();

  // ---------------- 中栏: Agent 瀑布流 ----------------
  const wfCard = h("div", { class: "card fill" });
  wfCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "Agent 工作流瀑布"),
    h("div", { class: "row gap-8" },
      h("span", { class: "badge", id: "wfStatus" }, "空闲"),
      h("button", { class: "btn btn-sm btn-ghost", onClick: () => clearWaterfall() },
        "清空"),
    ),
  ));
  const wfBody = h("div", { class: "card-body" });
  const wfList = h("div", { class: "waterfall" });
  wfBody.appendChild(wfList);
  wfCard.appendChild(wfBody);
  grid.appendChild(wfCard);

  // 空态
  wfList.appendChild(empty("等待开始", "选择左侧剧本或填写自定义 query 后点击运行", "play"));

  // ---------------- 右栏: 诊断详情 + 报告 ----------------
  const rightCol = h("div", { class: "col-fill" });
  grid.appendChild(rightCol);

  const detailCard = h("div", { class: "card fill" });
  detailCard.appendChild(h("div", { class: "card-head" }, h("h3", {}, "诊断与候选动作")));
  const detailBody = h("div", { class: "card-body" });
  detailCard.appendChild(detailBody);
  rightCol.appendChild(detailCard);
  detailBody.appendChild(empty("尚无数据", "诊断完成后会在这里显示根因 / 候选动作 / 执行结果"));

  const reportCard = h("div", { class: "card fill" });
  reportCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "复盘报告 (Markdown)"),
    h("button", { class: "btn btn-sm btn-ghost", id: "copyReportBtn",
      onClick: () => {
        const t = reportCard.dataset.text || "";
        if (!t) return;
        copyText(t, "报告已复制");
      } }, svgIcon("copy")),
  ));
  const reportBody = h("div", { class: "card-body" });
  reportCard.appendChild(reportBody);
  reportBody.appendChild(empty("报告未生成", "Reporter 跑完后会在这里渲染 Markdown"));
  rightCol.appendChild(reportCard);

  // ---------------- 触发诊断 ----------------
  async function startDiagnose(payload) {
    if (abortCtrl) {
      toast("已有诊断在进行,请先结束当前流", "", "warning");
      return;
    }
    currentSession = `inc-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    document.querySelector(".page-actions .badge")?.replaceChildren(`session: ${currentSession}`);

    clearWaterfall();
    detailBody.innerHTML = "";
    detailBody.appendChild(empty("诊断中", "等待 Agent 输出..."));
    reportBody.innerHTML = "";
    reportBody.appendChild(empty("等待 Reporter", ""));
    setStatus("running");

    const body = {
      session_id: currentSession,
      alert: payload.alert || null,
      query: payload.query || null,
      auto_fetch_alert: !payload.alert && !payload.query,
    };

    try {
      const stream = api.diagnose(body);
      for await (const evt of stream) {
        handleEvent(evt);
      }
    } catch (e) {
      pushStep("error", "exception", String(e));
      toast("SSE 流异常", String(e), "error");
    } finally {
      abortCtrl = null;
      setStatus("idle");
      refreshPending();
    }
  }

  // ---------------- SSE 事件分发 ----------------
  function handleEvent({ data }) {
    if (!data || typeof data !== "object") return;
    const type = data.type;
    const stage = data.stage;
    const msg = data.message || "";

    if (type === "interrupt") {
      currentInterruptPayload = data;
      pushStep("interrupt", stage, msg, jsonPreview({
        proposed_count: (data.proposed_actions || []).length,
      }));
      renderInterruptPanel(data);
      setStatus("awaiting approval");
      return;
    }
    if (type === "complete") {
      pushStep("complete", stage, msg);
      renderFinal(data);
      setStatus("done");
      return;
    }
    if (type === "error") {
      pushStep("error", stage, msg);
      return;
    }

    // route / agent_done / status / report
    let extra = null;
    if (stage === "historian") {
      extra = `召回: ${data.similar_incidents_count} similar / ${data.runbook_count} runbooks`;
    } else if (stage === "diagnostician") {
      const d = data.diagnosis || {};
      extra = `${d.root_cause?.slice(0, 100) || ""} (conf=${(d.confidence ?? 0).toFixed(2)})`;
      renderDiagnosis(d);
    } else if (stage === "remediator") {
      const acts = data.proposed_actions || [];
      extra = `${acts.length} 个候选动作`;
      renderActions(acts, []);
    } else if (stage === "human_review") {
      const aps = data.approved_actions || [];
      extra = `已批准 ${aps.length} 个`;
    } else if (stage === "executor") {
      const ex = data.execution_results || [];
      const ok = ex.filter((r) => r.success).length;
      extra = `执行: ${ok}/${ex.length} 成功`;
      renderExecutions(ex);
    } else if (type === "report" || stage === "reporter") {
      renderReport(data.report || "");
      extra = "复盘报告已生成";
    } else if (stage === "supervisor") {
      extra = `next → ${data.next_agent || "?"}`;
    }
    pushStep("info", stage || type, msg, extra);
  }

  // ---------------- Waterfall ----------------
  const stepsBuffer = []; // 缓存当前会话的瀑布步骤,便于恢复
  function pushStep(kind, stage, msg, detail) {
    stepsBuffer.push({ kind, stage, msg, detail, ts: Date.now() });
    if (wfList.firstChild?.classList?.contains("empty")) wfList.innerHTML = "";
    const stageLabel = STAGE_LABELS[stage] || stage || "step";
    const step = h("div", { class: `wf-step ${kind}` },
      h("div", { class: "wf-icon" }, svgIcon(iconForStage(stage, kind), 16)),
      h("div", {},
        h("div", { class: "wf-stage" }, stageLabel),
        h("div", { class: "wf-time mono" }, fmtTime()),
      ),
      h("div", {},
        h("div", { class: "wf-msg" }, msg || ""),
        detail && h("div", { class: "wf-detail" }, detail),
      ),
      kind === "interrupt" ? badge("人工审批", "warning") :
        kind === "complete" ? badge("完成", "success") :
          kind === "error" ? badge("错误", "danger") : null,
    );
    wfList.appendChild(step);
    wfBody.scrollTop = wfBody.scrollHeight;
  }

  function clearWaterfall() {
    stepsBuffer.length = 0;
    wfList.innerHTML = "";
    wfList.appendChild(empty("等待开始", "选择左侧剧本或填写自定义 query 后点击运行", "play"));
    detailBody.innerHTML = "";
    detailBody.appendChild(empty("尚无数据", ""));
    reportBody.innerHTML = "";
    reportBody.appendChild(empty("报告未生成", ""));
  }

  function setStatus(text) {
    const el = wfCard.querySelector("#wfStatus");
    if (!el) return;
    el.textContent = text;
    const cn = { running: "运行中", idle: "空闲", done: "完成",
      "awaiting approval": "等待审批", resuming: "恢复执行中" }[text] || text;
    el.textContent = cn;
    el.className = "badge " + (text === "running" ? "info" :
      text === "awaiting approval" ? "warning" :
      text === "done" ? "success" : "");
  }

  // ---------------- 详情面板 ----------------
  function renderDiagnosis(diag) {
    detailBody.innerHTML = "";
    if (!diag || !diag.root_cause) {
      detailBody.appendChild(empty("无诊断", ""));
      return;
    }
    const conf = Number(diag.confidence ?? 0);
    detailBody.appendChild(h("div", { class: "diag-card" },
      h("div", { class: "text-xs muted strong" }, "根因判断"),
      h("div", { class: "diag-rc mt-8" }, diag.root_cause),
      h("div", { class: "row between text-xs muted mt-12" },
        h("span", {}, `置信度 ${conf.toFixed(2)}`),
        h("span", {}, (diag.affected_services || []).join(", ")),
      ),
      h("div", { class: "diag-conf" }, h("span", { style: { width: `${conf * 100}%` } })),
      h("div", { class: "text-xs muted strong mt-12" }, "证据链"),
      h("ul", { class: "evidence-list" },
        ...(diag.evidence || []).map((ev) =>
          h("li", {}, h("span", { class: "src" }, `[${ev.source || "?"}] `), ev.fact || ""))
      ),
    ));
  }

  function renderActions(proposed, approved) {
    const actionsBlock = h("div", { class: "diag-card mt-12" },
      h("div", { class: "text-xs muted strong" }, `候选修复动作 (${proposed.length})`),
      h("div", { class: "action-list" },
        ...(proposed.map((a) =>
          h("div", { class: "action-row" },
            h("div", { class: "meta" },
              h("div", { class: "tool" }, a.tool_name || "?"),
              h("div", { class: "args" }, JSON.stringify(a.args || {})),
              a.rationale && h("div", { class: "rationale" }, a.rationale),
            ),
            riskBadge(a.risk_level),
          )))
      ),
    );
    detailBody.appendChild(actionsBlock);
  }

  function renderExecutions(execs) {
    const block = h("div", { class: "diag-card mt-12" },
      h("div", { class: "text-xs muted strong" }, `执行结果 (${execs.length})`),
      h("div", { class: "action-list" },
        ...(execs.map((r) =>
          h("div", { class: "action-row" },
            h("div", { class: "meta" },
              h("div", { class: "tool" }, r.tool_name || "?"),
              h("div", { class: "args" }, JSON.stringify(r.args || {})),
              r.error && h("div", { class: "rationale", style: { color: "var(--red)" } }, r.error),
            ),
            badge(r.success ? "成功" : "失败", r.success ? "success" : "danger"),
          )))
      ),
    );
    detailBody.appendChild(block);
  }

  function renderFinal(data) {
    if (data.diagnosis) renderDiagnosis(data.diagnosis);
    if (data.proposed_actions) renderActions(data.proposed_actions, data.approved_actions || []);
    if (data.execution_results) renderExecutions(data.execution_results);
    if (data.report) renderReport(data.report);
    // 缓存最近一次完成的结果,切走再回来可恢复显示;
    // 同时附带瀑布步骤 + session_id, 让 restoreLastSession 还原全貌。
    try {
      store.set("incidents.lastResult", {
        session_id: currentSession,
        finished_at: Date.now(),
        data,
        waterfall: stepsBuffer.slice(-200),
      });
    } catch (_) {}
  }

  function renderReport(text) {
    reportBody.innerHTML = "";
    if (!text) { reportBody.appendChild(empty("空报告", "")); return; }
    reportCard.dataset.text = text;
    reportBody.innerHTML = renderMarkdown(text);
  }

  // ---------------- HITL 审批面板 ----------------
  function renderInterruptPanel(payload) {
    detailBody.innerHTML = "";
    const proposed = payload.proposed_actions || [];
    const diagnosis = payload.diagnosis || {};
    detailBody.appendChild(h("div", { class: "badge warning mb-12" }, "等待人工审批"));

    if (diagnosis.root_cause) {
      detailBody.appendChild(h("div", { class: "diag-card mb-12" },
        h("div", { class: "text-xs muted strong" }, "根因诊断"),
        h("div", { class: "diag-rc mt-8" }, diagnosis.root_cause),
      ));
    }

    detailBody.appendChild(h("div", { class: "text-xs muted strong mb-8" },
      `候选动作 (${proposed.length}) — 勾选要批准的`));

    const checkboxes = [];
    proposed.forEach((a, i) => {
      const cb = h("input", { type: "checkbox", checked: a.risk_level !== "destructive" });
      checkboxes.push(cb);
      detailBody.appendChild(h("label", { class: "action-row",
        style: { cursor: "pointer", marginBottom: "8px" } },
        cb,
        h("div", { class: "meta" },
          h("div", { class: "tool" }, a.tool_name || "?"),
          h("div", { class: "args" }, JSON.stringify(a.args || {})),
          a.rationale && h("div", { class: "rationale" }, a.rationale),
        ),
        riskBadge(a.risk_level),
      ));
    });

    const reviewerInput = h("input", { class: "input mt-8", placeholder: "审批人 (可选)" });
    detailBody.appendChild(reviewerInput);
    const commentInput = h("input", { class: "input mt-8", placeholder: "备注 (可选)" });
    detailBody.appendChild(commentInput);

    detailBody.appendChild(h("div", { class: "row gap-8 mt-12" },
      h("button", { class: "btn btn-success flex-1",
        onClick: () => submitDecision(true, checkboxes, reviewerInput.value, commentInput.value) },
        svgIcon("check"), "批准选中"),
      h("button", { class: "btn btn-danger flex-1",
        onClick: () => submitDecision(false, [], reviewerInput.value, commentInput.value) },
        svgIcon("x"), "全部拒绝"),
    ));
  }

  async function submitDecision(approve, checkboxes, reviewer, comment) {
    const indices = approve
      ? checkboxes.map((cb, i) => cb.checked ? i : -1).filter((i) => i >= 0)
      : [];
    if (approve && !indices.length) {
      return toast("请至少选中一个动作,或点击全部拒绝", "", "warning");
    }
    // 提交后立刻清掉 HITL 面板,避免"等待人工审批"残留
    currentInterruptPayload = null;
    detailBody.innerHTML = "";
    detailBody.appendChild(loading(approve
      ? `已批准 ${indices.length} 个动作,执行中...`
      : "已全部拒绝,生成复盘中..."));
    pushStep("resume", "resuming",
      approve ? `已批准 ${indices.length} 个动作` : "已全部拒绝",
      reviewer ? `审批人: ${reviewer}` : "");
    setStatus("resuming");
    // 乐观从待审批列表里移除本 session,并稍后再 refresh 拉一次
    optimisticRemovePending(currentSession);
    try {
      const stream = api.approve({
        session_id: currentSession, approve,
        selected_indices: indices,
        comment: comment || "",
        reviewer: reviewer || "console-user",
      });
      for await (const evt of stream) handleEvent(evt);
    } catch (e) {
      toast("Resume 失败", String(e), "error");
    } finally {
      refreshPending();
    }
  }

  function optimisticRemovePending(sessionId) {
    if (!sessionId) return;
    const row = pendingBody.querySelector(`[data-session="${CSS.escape(sessionId)}"]`);
    if (row) row.remove();
    if (!pendingBody.querySelector("[data-session]")) {
      pendingBody.innerHTML = "";
      pendingBody.appendChild(empty("空", "暂无待审批 session"));
    }
  }

  // 如果 URL 带 session_id 且对应 session 处于 pending,自动加载审批
  if (params.session_id) {
    api.pendingGet(params.session_id).then((detail) => {
      currentSession = params.session_id;
      currentInterruptPayload = detail;
      renderInterruptPanel(detail);
      pushStep("interrupt", "awaiting_approval",
        `加载已暂停 session ${params.session_id}`,
        `候选数: ${(detail.proposed_actions || []).length}`);
    }).catch(() => { /* 不在 pending,忽略 */ });
  } else {
    // 没指定 session → 尝试恢复上次的诊断结果 (切走再回来不丢内容)
    restoreLastResult();
  }

  function restoreLastResult() {
    const last = store.get("incidents.lastResult");
    if (!last || !last.data) return;
    currentSession = last.session_id || currentSession;
    document.querySelector(".page-actions .badge")
      ?.replaceChildren(`session: ${currentSession}`);
    // 还原瀑布
    wfList.innerHTML = "";
    (last.waterfall || []).forEach((s) =>
      pushStep(s.kind, s.stage, s.msg, s.detail));
    if (!last.waterfall?.length) {
      wfList.appendChild(empty("等待开始",
        "选择左侧剧本或填写自定义 query 后点击运行", "play"));
    } else {
      pushStep("info", "diagnosis_complete",
        "已从本地缓存恢复上次诊断结果",
        "重新「运行诊断」会覆盖此内容");
    }
    renderFinal(last.data);
    setStatus("done");
  }

  return {
    node: root,
    cleanup() {
      if (abortCtrl) try { abortCtrl.abort(); } catch (_) {}
    },
  };
}

function iconForStage(stage, kind) {
  if (kind === "interrupt") return "alert";
  if (kind === "error") return "alert";
  if (kind === "complete") return "check";
  const map = {
    initializing: "play",
    supervisor: "graph",
    historian: "search",
    diagnostician: "brain",
    remediator: "bolt",
    human_review: "shield",
    awaiting_approval: "shield",
    executor: "bolt",
    reporter: "copy",
    diagnosis_complete: "check",
    resuming: "refresh",
  };
  return map[stage] || "graph";
}

function jsonPreview(obj) {
  return JSON.stringify(obj);
}
