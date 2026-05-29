/**
 * 故障档案 (History) — 已完成 SRE 诊断 session 的持久化查看 / 下载入口。
 *
 * 两栏布局
 * ========
 * 左:筛选 + session 列表 (root_cause / 时间 / 执行结果)
 * 右:选中 session 的完整详情 + 复盘报告 Markdown + 下载按钮
 *
 * 数据源
 * ======
 * GET /api/sre/history            列表 (摘要)
 * GET /api/sre/history/{sid}      完整详情
 * GET /api/sre/history/{sid}/report.md   Markdown 下载
 */

import { api } from "../api.js";
import {
  h, badge, riskBadge, empty, loading, svgIcon, toast,
  jsonBlock, renderMarkdown, copyText,
} from "../ui.js";
import { router } from "../router.js";

const STATUS_LABEL = {
  completed: "已完成",
  error: "异常",
};

export default function HistoryPage(params = {}) {
  const root = h("div", { class: "page" });

  // ---- Header ----
  const reloadBtn = h("button", { class: "btn btn-sm btn-ghost" },
    svgIcon("refresh"), "刷新");
  root.appendChild(h("div", { class: "page-head" },
    h("div", {},
      h("h2", {}, "故障档案"),
      h("div", { class: "page-sub" },
        "所有已完成的多 Agent SRE 诊断会自动归档于此,可回看 / 下载 Markdown 报告"),
    ),
    h("div", { class: "page-actions" },
      h("span", { class: "badge", id: "histTotal" }, "0 条"),
      reloadBtn,
    ),
  ));

  // ---- Body ----
  const grid = h("div", { class: "page-body",
    style: { display: "grid",
             gridTemplateColumns: "minmax(360px, 420px) 1fr",
             gap: "16px",
             alignItems: "start" } });
  root.appendChild(grid);

  // 左:列表
  const listCard = h("div", { class: "card" });
  const filterInput = h("input", { class: "input",
    placeholder: "搜索 session_id / 根因 / 类别",
    oninput: (e) => { filterText = e.target.value.toLowerCase(); renderList(); },
  });
  listCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "诊断历史"),
  ));
  listCard.appendChild(h("div", { class: "card-body tight" },
    h("div", { style: { padding: "0 12px 12px" } }, filterInput)));
  const listBody = h("div", { class: "card-body tight",
    style: { maxHeight: "calc(100vh - 280px)", overflowY: "auto" } },
    loading("加载历史..."));
  listCard.appendChild(listBody);
  grid.appendChild(listCard);

  // 右:详情
  const detailCard = h("div", { class: "card" });
  const detailHead = h("div", { class: "card-head" },
    h("h3", { id: "detailTitle" }, "选择左侧条目查看详情"),
    h("div", { class: "row gap-8" },
      h("button", { class: "btn btn-sm btn-ghost", id: "btnCopySid", disabled: true },
        svgIcon("copy"), "复制 session_id"),
      h("a", { class: "btn btn-sm btn-primary", id: "btnDownload",
        target: "_blank", rel: "noopener" }, svgIcon("external"), "下载 .md"),
    ),
  );
  detailCard.appendChild(detailHead);
  const detailBody = h("div", { class: "card-body" }, empty("尚未选中",
    "左侧点击任意 session 查看 alert / 根因 / 动作 / 执行 / 复盘报告"));
  detailCard.appendChild(detailBody);
  grid.appendChild(detailCard);

  // ---- State ----
  let allItems = [];        // 当前页全量(列表摘要)
  let filterText = "";
  let selectedSid = params.session_id || null;

  function renderList() {
    listBody.innerHTML = "";
    const items = filterText
      ? allItems.filter((it) =>
          (it.session_id || "").toLowerCase().includes(filterText) ||
          (it.root_cause || "").toLowerCase().includes(filterText) ||
          (it.root_cause_category || "").toLowerCase().includes(filterText))
      : allItems;
    if (!items.length) {
      listBody.appendChild(empty(
        filterText ? "无匹配结果" : "暂无历史档案",
        filterText ? "试试别的关键词" : "在「故障诊断」页跑一次后会自动出现"));
      return;
    }
    items.forEach((it) => {
      const isSel = it.session_id === selectedSid;
      const status = it.status || "completed";
      const tone = it.error ? "danger" : (status === "completed" ? "success" : "warning");
      const row = h("div", {
        class: "history-row" + (isSel ? " selected" : ""),
        onClick: () => selectSession(it.session_id),
      },
        h("div", { class: "row align-center gap-8" },
          badge(STATUS_LABEL[status] || status, tone),
          it.root_cause_category && badge(it.root_cause_category, "info"),
          it.confidence != null
            && h("span", { class: "text-xs muted" },
                 `conf ${Number(it.confidence).toFixed(2)}`),
        ),
        h("div", { class: "history-rc" },
          it.root_cause || it.alert_name || "(无根因摘要)"),
        h("div", { class: "row gap-8 text-xs muted mt-6 align-center wrap" },
          h("span", { class: "mono" }, (it.session_id || "").slice(0, 28) + "…"),
          h("span", {}, it.finished_at || ""),
          h("span", {},
            `执行 ${it.executed_ok || 0}/${it.executed_total || 0}`),
          it.has_report && badge("有报告", ""),
        ),
      );
      listBody.appendChild(row);
    });
  }

  async function selectSession(sid) {
    selectedSid = sid;
    renderList();
    detailHead.querySelector("#detailTitle").textContent = sid;
    detailHead.querySelector("#btnCopySid").disabled = false;
    detailHead.querySelector("#btnDownload").href = api.historyReportUrl(sid);
    detailBody.innerHTML = "";
    detailBody.appendChild(loading("加载详情..."));
    try {
      const rec = await api.historyGet(sid);
      renderDetail(rec);
    } catch (e) {
      detailBody.innerHTML = "";
      detailBody.appendChild(empty("加载失败", String(e), "alert"));
    }
  }

  function renderDetail(rec) {
    detailBody.innerHTML = "";
    const wrap = h("div", { class: "history-detail" });

    // 头条 meta
    const meta = h("div", { class: "row wrap gap-8 mb-12 align-center" },
      badge(STATUS_LABEL[rec.status] || rec.status,
        rec.error ? "danger" : (rec.status === "completed" ? "success" : "warning")),
      h("span", { class: "text-xs muted" }, `完成于 ${rec.finished_at}`),
      h("span", { class: "mono text-xs muted" }, rec.session_id),
    );
    wrap.appendChild(meta);

    // 告警 / query
    if (rec.alert || rec.query) {
      const box = h("div", { class: "diag-card mb-12" },
        h("div", { class: "text-xs muted strong" }, "诊断入口"),
        rec.alert && h("div", { class: "mt-8" },
          h("div", { class: "text-xs muted" }, "Alert"),
          jsonBlock(rec.alert)),
        rec.query && h("div", { class: "mt-8" },
          h("div", { class: "text-xs muted" }, "Query"),
          h("div", { class: "mono text-xs" }, rec.query)),
      );
      wrap.appendChild(box);
    }

    // 根因
    const diag = rec.diagnosis || {};
    if (diag.root_cause) {
      wrap.appendChild(h("div", { class: "diag-card mb-12" },
        h("div", { class: "text-xs muted strong" },
          `根因诊断 · 类别 ${diag.root_cause_category || "-"} · `
          + `置信度 ${(diag.confidence ?? 0).toFixed(2)}`),
        h("div", { class: "diag-rc mt-8" }, diag.root_cause),
        (diag.affected_services || []).length > 0
          && h("div", { class: "text-xs muted mt-8" },
            "受影响: ",
            ...(diag.affected_services || []).map((s) =>
              h("span", { class: "badge ml-6" }, s))),
      ));
    }

    // 提议 / 批准 / 执行
    const proposed = rec.proposed_actions || [];
    const approved = rec.approved_actions || [];
    const executions = rec.execution_results || [];
    if (proposed.length || executions.length) {
      const sec = h("div", { class: "mb-12" });
      sec.appendChild(h("div", { class: "text-xs muted strong mb-8" },
        `候选动作 ${proposed.length} · 批准 ${approved.length} · 执行 ${executions.length}`));
      const approvedKeys = new Set(approved.map((a) =>
        JSON.stringify([a.tool_name, a.args])));
      proposed.forEach((a) => {
        const wasApproved = approvedKeys.has(JSON.stringify([a.tool_name, a.args]));
        const ex = executions.find((e) =>
          e.tool_name === a.tool_name &&
          JSON.stringify(e.args || {}) === JSON.stringify(a.args || {}));
        const ok = ex ? (ex.ok || ex.success) : null;
        sec.appendChild(h("div", { class: "action-row" },
          h("div", { class: "meta" },
            h("div", { class: "tool" }, a.tool_name || "?"),
            h("div", { class: "args mono" }, JSON.stringify(a.args || {})),
            a.rationale && h("div", { class: "rationale" }, a.rationale),
            ex && h("div", { class: "rationale" },
              ok ? "✅ 执行成功" : "❌ 执行失败",
              ex.error && ` · ${ex.error}`),
          ),
          h("div", { class: "row gap-8 align-center" },
            riskBadge(a.risk_level),
            wasApproved ? badge("已批准", "success") : badge("未批准", ""),
          ),
        ));
      });
      wrap.appendChild(sec);
    }

    // 报告
    if (rec.report) {
      wrap.appendChild(h("div", { class: "card mb-12",
        style: { border: "1px solid var(--line-1)", borderRadius: "8px" } },
        h("div", { class: "card-head" },
          h("h3", {}, "复盘报告 (Markdown)"),
          h("button", { class: "btn btn-sm btn-ghost",
            onClick: () => copyText(rec.report).then(() =>
              toast("已复制 Markdown", "", "success", 2000)) },
            svgIcon("copy"), "复制原文"),
        ),
        h("div", { class: "card-body markdown-body",
          html: renderMarkdown(rec.report) }),
      ));
    } else {
      wrap.appendChild(empty("无复盘报告", "可能因 HITL 全部拒绝或异常退出未生成"));
    }

    // 错误
    if (rec.error) {
      wrap.appendChild(h("div", { class: "diag-card",
        style: { borderColor: "var(--red)" } },
        h("div", { class: "text-xs strong", style: { color: "var(--red)" } },
          "异常"),
        h("pre", { class: "mono text-xs mt-8",
          style: { whiteSpace: "pre-wrap" } }, rec.error)));
    }

    detailBody.appendChild(wrap);
  }

  async function refresh() {
    listBody.innerHTML = "";
    listBody.appendChild(loading("加载历史..."));
    try {
      const data = await api.historyList({ limit: 200 });
      allItems = data?.items || [];
      const totEl = root.querySelector("#histTotal");
      if (totEl) totEl.textContent = `${data?.total ?? allItems.length} 条`;
      renderList();
      // 自动选中 URL 指定的 session,或第一条
      const targetSid = selectedSid
        || (allItems[0] && allItems[0].session_id);
      if (targetSid) selectSession(targetSid);
    } catch (e) {
      listBody.innerHTML = "";
      listBody.appendChild(empty("加载失败", String(e), "alert"));
    }
  }

  reloadBtn.addEventListener("click", refresh);
  detailHead.querySelector("#btnCopySid").addEventListener("click", () => {
    if (selectedSid) {
      copyText(selectedSid).then(() =>
        toast("已复制", selectedSid, "success", 2000));
    }
  });
  // 详情未选中时禁用下载
  detailHead.querySelector("#btnDownload").addEventListener("click", (e) => {
    if (!selectedSid) { e.preventDefault(); toast("先选中条目", "", "warning"); }
  });

  refresh();

  return { node: root };
}
