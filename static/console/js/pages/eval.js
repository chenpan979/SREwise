/**
 * Eval Runner — 触发 SSE eval + 实时进度 + 历史结果。
 *
 * 三个区:
 * 1. 顶部统计 (上次结果 KPI)
 * 2. Case 列表 (可勾选,展示进度状态)
 * 3. 单 case drilldown (失败原因 / 推断的 root_cause / 提议工具)
 */

import { api } from "../api.js";
import {
  h, badge, empty, loading, svgIcon, toast, escapeHtml, jsonBlock,
  showModal, pct,
} from "../ui.js";

export default async function evalPage() {
  const root = h("div", { class: "page" });
  let scenarios = [];
  let scoreById = new Map();    // case_id -> CaseScore
  let runningCase = null;
  let aborted = false;

  root.appendChild(h("div", { class: "page-head" },
    h("div", {},
      h("h1", {}, "评测中心 · 多 Agent 跑分"),
      h("p", {}, "用 6 个 Golden Scenarios 量化诊断准确率 / 修复召回 / 安全门"),
    ),
    h("div", { class: "page-actions" },
      h("button", { class: "btn btn-primary", onClick: runSelected },
        svgIcon("play"), "运行选中"),
      h("button", { class: "btn", onClick: runAll },
        svgIcon("play"), "运行全部"),
    ),
  ));

  // Stats (从 last result 拿)
  const statRow = h("div", { class: "stat-grid" });
  root.appendChild(statRow);

  // 进度条
  const progress = h("div", { class: "progress",
    style: { display: "none" } }, h("span", { style: { width: "0%" } }));
  root.appendChild(progress);

  // 主区域: 列表 + 详情 上下布局,各自内部滚动
  const body = h("div", { class: "page-body" });
  root.appendChild(body);

  // 场景列表卡
  const listCard = h("div", { class: "card fill" });
  listCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "场景列表"),
    h("div", { class: "row gap-8" },
      h("label", { class: "row gap-6 text-xs muted",
        style: { cursor: "pointer" } },
        h("input", { type: "checkbox", id: "selAll",
          onChange: (e) => {
            listCard.querySelectorAll(".case-cb").forEach((cb) =>
              cb.checked = e.target.checked);
          },
        }),
        "全选"),
    ),
  ));
  const listBody = h("div", { class: "card-body tight" });
  listCard.appendChild(listBody);
  body.appendChild(listCard);

  // 详情卡
  const detailCard = h("div", { class: "card fill" });
  detailCard.appendChild(h("div", { class: "card-head" },
    h("h3", {}, "选中场景详情")));
  const detailBody = h("div", { class: "card-body" });
  detailCard.appendChild(detailBody);
  detailBody.appendChild(empty("点击场景行", "查看失败原因 / 提议工具 / 推断根因"));
  body.appendChild(detailCard);

  // 加载场景与最近结果
  await reload();

  async function reload() {
    listBody.innerHTML = ""; listBody.appendChild(loading());
    try {
      const data = await api.evalScenarios();
      scenarios = data?.scenarios || [];
      renderList();

      const last = await api.evalLast();
      if (last?.result) {
        renderStats(last.result);
        (last.result.by_case || []).forEach((c) => scoreById.set(c.case_id, c));
        renderList();
      } else {
        renderStats(null);
      }
    } catch (e) {
      listBody.innerHTML = "";
      listBody.appendChild(empty("加载失败", String(e), "alert"));
    }
  }

  function renderStats(r) {
    statRow.innerHTML = "";
    if (!r) {
      statRow.appendChild(statBox("通过率", "—", "未跑过 eval"));
      return;
    }
    statRow.appendChild(statBox("通过率", pct(r.pass_rate),
      `${r.passed}/${r.total} 个场景`));
    statRow.appendChild(statBox("根因命中", pct(r.rc_hit_rate), ""));
    statRow.appendChild(statBox("修复召回", pct(r.action_hit_rate), ""));
    statRow.appendChild(statBox("平均置信度", (r.avg_confidence || 0).toFixed(2), ""));
    statRow.appendChild(statBox("平均延迟",
      `${(r.avg_latency_seconds || 0).toFixed(1)}s`, "壁钟时间"));
    statRow.appendChild(statBox("安全违规", String(r.safety_violations_total),
      r.safety_violations_total === 0 ? "✅" : "⚠️"));
  }

  function renderList() {
    listBody.innerHTML = "";
    listBody.appendChild(h("div", { class: "eval-row head" },
      h("div"), h("div", {}, "ID / 描述"), h("div", {}, "审批策略"),
      h("div", {}, "状态"), h("div", { class: "right" }, "耗时")));
    scenarios.forEach((s) => listBody.appendChild(renderRow(s)));
  }

  function renderRow(s) {
    const score = scoreById.get(s.id);
    const status = runningCase === s.id ? "running" :
      score?.success === true ? "pass" :
      score?.success === false ? "fail" :
      "idle";
    const row = h("div", { class: `eval-row ${status === "pass" ? "pass" : status === "fail" ? "fail" : ""}`,
      style: { cursor: "pointer" },
      onClick: (e) => {
        if (e.target.tagName === "INPUT") return;
        renderDetail(s);
      },
    },
      h("input", { type: "checkbox", class: "case-cb", "data-case": s.id,
        checked: status === "idle" || status === "running" }),
      h("div", {},
        h("div", { class: "id" }, s.id),
        h("div", { class: "desc truncate" }, s.description),
      ),
      badge(s.approval_policy, "info"),
      statusBadge(status),
      h("div", { class: "right text-xs mono muted" },
        score ? `${score.latency_seconds.toFixed(1)}s` : "—"),
    );
    return row;
  }

  function statusBadge(status) {
    if (status === "pass") return badge("通过", "success");
    if (status === "fail") return badge("未通过", "danger");
    if (status === "running") return h("span", { class: "row gap-6" },
      h("span", { class: "spinner" }), "运行中");
    return badge("待运行");
  }

  function renderDetail(s) {
    const score = scoreById.get(s.id);
    detailBody.innerHTML = "";
    detailBody.appendChild(h("div", { class: "row between mb-12" },
      h("div", {},
        h("h3", { style: { margin: "0 0 4px" } }, s.id),
        h("div", { class: "muted text-sm" }, s.description),
      ),
      score ? statusBadge(score.success ? "pass" : "fail") : badge("未运行"),
    ));

    if (!score) {
      detailBody.appendChild(empty("尚未运行", "点击运行后这里会显示评分细节"));
      return;
    }

    // KPI 卡
    detailBody.appendChild(h("div", { class: "stat-grid mb-12" },
      kvBox("推断根因", score.root_cause_inferred,
        score.root_cause_hit ? "success" : "danger"),
      kvBox("修复召回", score.action_recall_hit ? "✓" : "✗",
        score.action_recall_hit ? "success" : "danger"),
      kvBox("禁用工具违规", String(score.forbidden_violations.length),
        score.forbidden_violations.length === 0 ? "success" : "danger"),
      kvBox("耗时", `${score.latency_seconds.toFixed(1)}s`),
    ));

    // 失败原因
    if (score.reasons?.length) {
      detailBody.appendChild(h("div", { class: "diag-card mb-12" },
        h("div", { class: "text-xs muted strong" }, "失败原因"),
        h("ul", {},
          ...score.reasons.map((r) =>
            h("li", { class: "text-sm", style: { color: "var(--red)" } }, r))
        ),
      ));
    }

    // 诊断 root_cause text
    if (score.diagnosis_root_cause_text) {
      detailBody.appendChild(h("div", { class: "diag-card mb-12" },
        h("div", { class: "text-xs muted strong" }, "诊断输出的根因文本"),
        h("div", { class: "diag-rc mt-8" }, score.diagnosis_root_cause_text),
      ));
    }

    // 提议 / 执行工具
    detailBody.appendChild(h("div", { class: "row gap-12" },
      h("div", { class: "diag-card flex-1" },
        h("div", { class: "text-xs muted strong" }, "提议的工具"),
        h("div", { class: "mt-8 row wrap gap-6" },
          ...(score.proposed_tools || []).map((t) => badge(t, "purple")),
          (score.proposed_tools || []).length === 0 ? badge("无") : null,
        ),
      ),
      h("div", { class: "diag-card flex-1" },
        h("div", { class: "text-xs muted strong" }, "实际执行的工具"),
        h("div", { class: "mt-8 row wrap gap-6" },
          ...(score.executed_tools || []).map((t) => badge(t, "success")),
          (score.executed_tools || []).length === 0 ? badge("无") : null,
        ),
      ),
    ));

    if (score.errors?.length) {
      detailBody.appendChild(h("div", { class: "diag-card mt-12" },
        h("div", { class: "text-xs muted strong" }, "运行时错误"),
        h("ul", {},
          ...score.errors.map((e) => h("li", { class: "text-sm" }, e)),
        ),
      ));
    }

    // expected
    detailBody.appendChild(h("details", { class: "mt-12" },
      h("summary", { class: "muted text-sm",
        style: { cursor: "pointer" } }, "预期值 (来自 scenarios.json)"),
      jsonBlock({
        root_cause_categories: s.expected_categories,
        must_include_any_tool: s.must_include_any_tool,
        approval_policy: s.approval_policy,
      }),
    ));
  }

  // ---------------- 运行 ----------------
  async function runSelected() {
    const ids = [...listBody.querySelectorAll(".case-cb:checked")]
      .map((cb) => cb.dataset.case);
    if (!ids.length) return toast("请先选中至少一个 case", "", "warning");
    await runIds(ids);
  }
  async function runAll() { await runIds(scenarios.map((s) => s.id)); }

  async function runIds(ids) {
    aborted = false;
    progress.style.display = "block";
    const span = progress.querySelector("span");
    span.style.width = "0%";
    let done = 0;
    scoreById = new Map();   // 重置
    renderList();
    detailBody.innerHTML = ""; detailBody.appendChild(loading("Eval 跑分中,期间可点击 case 查看进度"));
    try {
      const stream = api.evalRun({ case_ids: ids });
      for await (const evt of stream) {
        if (aborted) break;
        if (evt.event === "case_start") {
          runningCase = evt.data?.case_id;
          renderList();
        } else if (evt.event === "case_done") {
          const sc = evt.data;
          if (sc?.case_id) {
            scoreById.set(sc.case_id, sc);
            done++;
            span.style.width = `${(done / ids.length) * 100}%`;
            renderList();
          }
        } else if (evt.event === "done") {
          const r = evt.data;
          renderStats(r);
          toast("Eval 完成",
            `${r.passed}/${r.total} 通过 · ${pct(r.pass_rate)}`,
            r.passed === r.total ? "success" : "warning");
        } else if (evt.event === "start") {
          // header 已渲染
        }
      }
    } catch (e) {
      toast("Eval 异常", String(e), "error");
    } finally {
      runningCase = null;
      progress.style.display = "none";
      renderList();
      detailBody.innerHTML = "";
      detailBody.appendChild(empty("点击 case 行查看详情", ""));
    }
  }

  return {
    node: root,
    cleanup() { aborted = true; },
  };
}

// ============================================================
function statBox(label, value, meta) {
  return h("div", { class: "stat" },
    h("div", { class: "stat-label" }, label),
    h("div", { class: "stat-value" }, String(value)),
    meta && h("div", { class: "stat-meta" }, meta),
  );
}
function kvBox(k, v, kind) {
  const color = kind === "success" ? "var(--green)" :
    kind === "danger" ? "var(--red)" : "var(--fg-0)";
  return h("div", { class: "stat" },
    h("div", { class: "stat-label" }, k),
    h("div", { class: "stat-value", style: { color } }, String(v)),
  );
}
