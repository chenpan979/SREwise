/**
 * SREwise Console — bootstrap.
 *
 * 启动时
 * ======
 * 1. 主题恢复 (localStorage)
 * 2. 注册路由 → 按需加载 pages
 * 3. 启动后台 health 轮询 + pending count 轮询
 * 4. 全局快捷键 (g d / g i / g e / g k / g g)
 */

import { router } from "./router.js";
import { store } from "./store.js";
import { api } from "./api.js";
import { toast } from "./ui.js";

// ============================================================
// 主题
// ============================================================
const themeBtn = document.getElementById("themeToggle");
const savedTheme = localStorage.getItem("srewise.theme") || "dark";
document.documentElement.setAttribute("data-theme", savedTheme);
store.set("theme", savedTheme);
themeBtn.addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("srewise.theme", next);
  store.set("theme", next);
});

// ============================================================
// Health 轮询
// ============================================================
const healthBadge = document.getElementById("healthBadge");
async function pollHealth() {
  try {
    const data = await api.health();
    store.set("health", data);
    const lf = data?.data?.langfuse;
    if (lf?.message?.startsWith?.("已接入 ")) {
      const host = lf.message.replace("已接入 ", "").trim();
      store.set("langfuse.host", host);
      const link = document.getElementById("langfuseLink");
      if (link) link.href = host;
    }
    const status = data?.data?.status;
    if (status === "healthy") setHealthBadge("ok", "正常");
    else setHealthBadge("degraded", status === "degraded" ? "降级" : (status || "降级"));
  } catch (e) {
    setHealthBadge("down", "失联");
  }
}
function setHealthBadge(kind, text) {
  healthBadge.classList.remove("ok", "degraded", "down");
  healthBadge.classList.add(kind);
  healthBadge.querySelector(".text").textContent = text;
}
healthBadge.addEventListener("click", () => router.navigate("dashboard"));

setInterval(pollHealth, 12_000);
pollHealth();

// ============================================================
// Pending 数轮询 (顶部 Incidents 红点)
// ============================================================
const navPending = document.getElementById("navPendingCount");
async function pollPending() {
  try {
    const data = await api.pendingList();
    const items = data?.items || [];
    store.set("pending.items", items);
    store.set("pending.count", items.length);
    if (items.length > 0) {
      navPending.hidden = false;
      navPending.textContent = String(items.length);
    } else {
      navPending.hidden = true;
    }
  } catch (_) { /* 静默 */ }
}
setInterval(pollPending, 8_000);
pollPending();

// ============================================================
// 注册页面 (动态导入)
// ============================================================
async function registerPages() {
  const pages = ["dashboard", "incidents", "history", "kg", "graphrag", "eval"];
  for (const name of pages) {
    try {
      const mod = await import(`./pages/${name}.js`);
      router.register(name, mod.default);
    } catch (e) {
      console.error(`[main] page ${name} 加载失败`, e);
      router.register(name, () => {
        const div = document.createElement("div");
        div.className = "empty";
        div.innerHTML = `<div class="empty-title">页面 ${name} 加载失败</div><div class="empty-sub">${String(e)}</div>`;
        return div;
      });
    }
  }
  router.resolve();
}
registerPages();

// ============================================================
// 全局快捷键 (gd / gi / gk / gg / ge)
// ============================================================
let _gPressed = false;
let _gTimer = null;
window.addEventListener("keydown", (e) => {
  // 忽略输入元素中的键
  if (["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  if (e.key === "g" && !_gPressed) {
    _gPressed = true;
    clearTimeout(_gTimer);
    _gTimer = setTimeout(() => { _gPressed = false; }, 800);
    return;
  }
  if (_gPressed) {
    const map = { d: "dashboard", i: "incidents", h: "history",
      k: "kg", g: "graphrag", e: "eval" };
    const target = map[e.key];
    if (target) { e.preventDefault(); router.navigate(target); }
    _gPressed = false;
    clearTimeout(_gTimer);
  }
});

// ============================================================
// 全局搜索 / 命令面板
// ============================================================
const searchInput = document.getElementById("globalSearchInput");
const searchPop = document.getElementById("globalSearchPop");

const NAV_TARGETS = [
  { keys: ["dashboard", "看板", "dash", "d"], route: "dashboard", label: "概览看板", hint: "g d" },
  { keys: ["incidents", "故障", "诊断", "i"], route: "incidents", label: "故障诊断", hint: "g i" },
  { keys: ["history", "档案", "历史", "h"], route: "history", label: "故障档案", hint: "g h" },
  { keys: ["kg", "图谱", "知识图谱", "k"], route: "kg", label: "知识图谱", hint: "g k" },
  { keys: ["graphrag", "rag", "召回", "r", "g"], route: "graphrag", label: "GraphRAG 调试", hint: "g g" },
  { keys: ["eval", "评测", "跑分", "e"], route: "eval", label: "评测中心", hint: "g e" },
];

function renderSearchPop(q) {
  searchPop.innerHTML = "";
  const query = (q || "").trim().toLowerCase();
  const items = [];

  // 跳转命令
  NAV_TARGETS.forEach((t) => {
    if (!query || t.keys.some((k) => k.toLowerCase().includes(query)) ||
        t.label.includes(query)) {
      items.push({ kind: "nav", label: `跳转 · ${t.label}`, hint: t.hint, route: t.route });
    }
  });

  // 故障搜索 (作为一项触发 incidents 页带 query)
  if (query.length >= 2) {
    items.unshift({
      kind: "diag", label: `在 故障诊断 中以 "${query}" 发起诊断`,
      hint: "Enter", route: "incidents", query,
    });
  }

  if (!items.length) {
    searchPop.innerHTML = `<div class="search-empty">无匹配,试试 "故障" / "评测" / "g d" 等</div>`;
    searchPop.hidden = false;
    return;
  }

  items.slice(0, 8).forEach((it, idx) => {
    const row = document.createElement("div");
    row.className = "search-row" + (idx === 0 ? " active" : "");
    row.dataset.idx = String(idx);
    row.innerHTML = `<span class="t">${it.label}</span><kbd>${it.hint}</kbd>`;
    row.addEventListener("mousedown", (e) => { e.preventDefault(); runItem(it); });
    searchPop.appendChild(row);
  });
  searchPop._items = items.slice(0, 8);
  searchPop._active = 0;
  searchPop.hidden = false;
}
function runItem(it) {
  closeSearch();
  if (it.kind === "diag") {
    router.navigate(it.route, { query: it.query });
  } else {
    router.navigate(it.route);
  }
}
function closeSearch() {
  searchPop.hidden = true;
  searchInput.blur();
}
searchInput.addEventListener("focus", () => renderSearchPop(searchInput.value));
searchInput.addEventListener("input", () => renderSearchPop(searchInput.value));
searchInput.addEventListener("blur", () => setTimeout(closeSearch, 120));
searchInput.addEventListener("keydown", (e) => {
  const items = searchPop._items || [];
  if (e.key === "Escape") { closeSearch(); return; }
  if (e.key === "ArrowDown") {
    e.preventDefault();
    searchPop._active = Math.min((searchPop._active ?? 0) + 1, items.length - 1);
    refreshActive();
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    searchPop._active = Math.max((searchPop._active ?? 0) - 1, 0);
    refreshActive();
  } else if (e.key === "Enter") {
    e.preventDefault();
    const it = items[searchPop._active ?? 0];
    if (it) runItem(it);
  }
});
function refreshActive() {
  searchPop.querySelectorAll(".search-row").forEach((el, i) => {
    el.classList.toggle("active", i === searchPop._active);
  });
}
// ⌘K / Ctrl+K 聚焦搜索
window.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    searchInput.focus();
    searchInput.select();
  }
});

// 显示首次欢迎提示
if (!sessionStorage.getItem("srewise.welcomed")) {
  toast("欢迎回来", "试试快捷键: g d / g i / g e", "info", 4500);
  sessionStorage.setItem("srewise.welcomed", "1");
}
