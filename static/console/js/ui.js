/**
 * 共享 UI 组件层 — 全部纯 DOM,无框架。
 *
 * 设计原则
 * ========
 * - 函数式 ("create*" / "show*"): 调用即返回 DOM 节点或副作用
 * - 不持有跨调用状态 (除 Toast 队列)
 * - 所有动画/过渡都靠 CSS,JS 只控类
 */

// ============================================================
// 模板字符串助手
// ============================================================
export const h = (tag, attrs = {}, ...children) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    if (typeof c === "string" || typeof c === "number") el.appendChild(document.createTextNode(c));
    else el.appendChild(c);
  }
  return el;
};

export const escapeHtml = (s) => String(s ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#39;");

export const fmtTime = (d = new Date()) => {
  const z = (n) => String(n).padStart(2, "0");
  return `${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
};

export const pct = (x, digits = 1) => `${(x * 100).toFixed(digits)}%`;

// ============================================================
// Toast
// ============================================================
let _toastRoot;
export function toast(title, body = "", type = "info", duration = 3500) {
  if (!_toastRoot) _toastRoot = document.getElementById("toasts");
  const el = h("div", { class: `toast ${type}` },
    h("div", { class: "flex-1" },
      h("div", { class: "title" }, title),
      body && h("div", { class: "body" }, body),
    ),
    h("button", { class: "iconbtn", onClick: () => el.remove() },
      svgIcon("close")),
  );
  _toastRoot.appendChild(el);
  if (duration > 0) setTimeout(() => el.remove(), duration);
  return el;
}

// ============================================================
// Modal
// ============================================================
export function showModal({ title, body, footer, width, onClose }) {
  const root = document.getElementById("modal-root");
  const backdrop = h("div", { class: "modal-backdrop" });
  const modal = h("div", { class: "modal", style: width ? { width: `min(${width}px, 92vw)` } : {} });
  const close = () => {
    backdrop.remove(); modal.remove();
    if (typeof onClose === "function") onClose();
  };
  modal.appendChild(h("div", { class: "modal-head" },
    h("h3", {}, title || ""),
    h("button", { class: "iconbtn", onClick: close }, svgIcon("close")),
  ));
  const bodyEl = h("div", { class: "modal-body" });
  if (body instanceof Node) bodyEl.appendChild(body);
  else if (typeof body === "string") bodyEl.innerHTML = body;
  modal.appendChild(bodyEl);
  if (footer) {
    const f = h("div", { class: "modal-foot" });
    if (footer instanceof Node) f.appendChild(footer);
    else if (Array.isArray(footer)) footer.forEach((n) => f.appendChild(n));
    modal.appendChild(f);
  }
  backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function escHandler(e) {
    if (e.key === "Escape") {
      close();
      document.removeEventListener("keydown", escHandler);
    }
  });
  root.appendChild(backdrop);
  root.appendChild(modal);
  return { close, modal, body: bodyEl };
}

// ============================================================
// 状态空态
// ============================================================
export const empty = (title, sub = "", icon = "inbox") =>
  h("div", { class: "empty" },
    svgIcon(icon, 32),
    h("div", { class: "empty-title" }, title),
    sub && h("div", { class: "empty-sub" }, sub),
  );

export const loading = (msg = "加载中") =>
  h("div", { class: "empty" },
    h("span", { class: "spinner" }),
    h("div", { class: "empty-sub" }, msg),
  );

// ============================================================
// 徽章
// ============================================================
export const badge = (text, kind = "") => h("span", { class: `badge ${kind}` }, text);

export const riskBadge = (risk) => {
  const map = { read: "info", write: "warning", destructive: "danger" };
  return badge(risk || "?", map[risk] || "");
};

export const severityBadge = (sev) => {
  const map = { critical: "danger", warning: "warning", info: "info" };
  return badge(sev || "info", map[sev] || "info");
};

// ============================================================
// SVG 图标 (内联,无依赖)
// ============================================================
const ICONS = {
  close: '<path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/>',
  check: '<path d="m9 16.17-3.88-3.88a.996.996 0 1 0-1.41 1.41l4.59 4.59c.39.39 1.02.39 1.41 0L20.71 7.71a.996.996 0 1 0-1.41-1.41L9 16.17z" fill="currentColor"/>',
  x: '<path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" fill="currentColor"/>',
  play: '<path d="M8 5v14l11-7z" fill="currentColor"/>',
  pause: '<path d="M6 4h4v16H6zM14 4h4v16h-4z" fill="currentColor"/>',
  refresh: '<path d="M17.65 6.35A7.96 7.96 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A6 6 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/>',
  alert: '<path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z" fill="currentColor"/>',
  brain: '<path d="M19.41 7.41A2 2 0 0 0 18 7c-.42 0-.81.11-1.16.31A4.99 4.99 0 0 0 13 5c-.92 0-1.78.26-2.5.7A4.99 4.99 0 0 0 7 5C4.24 5 2 7.24 2 10v4c0 2.76 2.24 5 5 5 1.46 0 2.78-.62 3.7-1.62a4.99 4.99 0 0 0 6.6 0A4.98 4.98 0 0 0 21 14v-4c0-1.04-.32-2-.85-2.79l-.74.2zM7 17c-1.66 0-3-1.34-3-3v-4c0-1.66 1.34-3 3-3s3 1.34 3 3v4c0 1.66-1.34 3-3 3zm10 0c-1.66 0-3-1.34-3-3v-4c0-1.66 1.34-3 3-3s3 1.34 3 3v4c0 1.66-1.34 3-3 3z" fill="currentColor"/>',
  inbox: '<path d="M19 3H5c-1.11 0-1.99.89-1.99 2L3 19c0 1.1.88 2 1.99 2H19c1.1 0 2-.9 2-2V5c0-1.11-.9-2-2-2zm0 12h-4c0 1.66-1.35 3-3 3s-3-1.34-3-3H4.99V5H19v10z" fill="currentColor"/>',
  bolt: '<path d="M7 2v11h3v9l7-12h-4l3-8z" fill="currentColor"/>',
  shield: '<path d="M12 1 3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z" fill="currentColor"/>',
  graph: '<path d="M3 3h2v18H3V3zm6 8h2v10H9V11zm6-6h2v16h-2V5zm6 4h2v12h-2V9z" fill="currentColor"/>',
  search: '<circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2" fill="none"/><path d="m21 21-3-3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  copy: '<path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z" fill="currentColor"/>',
  external: '<path d="M19 19H5V5h7V3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z" fill="currentColor"/>',
  "chevron-down": '<path d="M7.41 8.59 12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z" fill="currentColor"/>',
  "chevron-right": '<path d="M8.59 16.59 13.17 12 8.59 7.41 10 6l6 6-6 6-1.41-1.41z" fill="currentColor"/>',
};
export function svgIcon(name, size = 16) {
  const inner = ICONS[name] || ICONS.inbox;
  const wrap = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  wrap.setAttribute("viewBox", "0 0 24 24");
  wrap.setAttribute("width", size);
  wrap.setAttribute("height", size);
  wrap.classList.add("icon");
  wrap.innerHTML = inner;
  return wrap;
}

// ============================================================
// Markdown 渲染 (依赖 marked,加载失败则降级 escape)
// ============================================================
export function renderMarkdown(text) {
  if (!text) return "";
  if (window.marked) {
    try { return window.marked.parse(String(text)); } catch (_) {}
  }
  return `<pre>${escapeHtml(text)}</pre>`;
}

// ============================================================
// 复制到剪贴板
// ============================================================
export async function copyText(text, msg = "已复制") {
  try {
    await navigator.clipboard.writeText(text);
    toast(msg, "", "success", 1500);
  } catch (e) {
    toast("复制失败", String(e), "error");
  }
}

// ============================================================
// 安全 JSON 渲染 (折叠大对象)
// ============================================================
export function jsonBlock(obj, maxLen = 6000) {
  let s;
  try { s = JSON.stringify(obj, null, 2); } catch (_) { s = String(obj); }
  if (s.length > maxLen) s = s.slice(0, maxLen) + `\n... (truncated ${s.length - maxLen} chars)`;
  return h("pre", { class: "codeblock" }, s);
}
