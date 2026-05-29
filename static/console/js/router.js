/**
 * 极简 hash router。
 *
 * 路由约定: #/<page>?<query>
 *   #/dashboard
 *   #/incidents?session_id=xxx
 *
 * 注册一个 page handler:
 *   router.register("dashboard", async (params) => {
 *     // 返回 DOM 节点 (或 Promise)
 *   });
 *
 * 路由切换会调用 handler,把返回的节点挂到 #view 容器。
 */

const _handlers = new Map();
let _currentPage = null;

export const router = {
  register(name, handler) { _handlers.set(name, handler); },

  async navigate(name, params = {}) {
    const qs = new URLSearchParams(params).toString();
    location.hash = `#/${name}${qs ? `?${qs}` : ""}`;
  },

  async resolve() {
    const view = document.getElementById("view");
    if (!view) return;
    const hash = location.hash || "#/dashboard";
    const m = /^#\/?([^?]*)(?:\?(.*))?$/.exec(hash);
    const page = (m && m[1]) || "dashboard";
    const params = Object.fromEntries(new URLSearchParams((m && m[2]) || ""));

    // 高亮侧边栏
    document.querySelectorAll(".nav-item[data-route]").forEach((el) => {
      el.classList.toggle("active", el.dataset.route === page);
    });

    const handler = _handlers.get(page) || _handlers.get("dashboard");
    if (!handler) {
      view.innerHTML = `<div class="empty"><div class="empty-title">404</div><div class="empty-sub">页面 ${page} 未注册</div></div>`;
      return;
    }

    // 清理旧页面
    if (_currentPage && typeof _currentPage.cleanup === "function") {
      try { _currentPage.cleanup(); } catch (_) {}
    }
    view.innerHTML = "";
    view.appendChild(_loadingPlaceholder());

    try {
      const ret = await handler(params);
      view.innerHTML = "";
      if (ret instanceof Node) view.appendChild(ret);
      else if (ret && ret.node instanceof Node) {
        view.appendChild(ret.node);
        _currentPage = ret;
      }
      else view.innerHTML = "<!-- empty page -->";
    } catch (e) {
      console.error("[router] page render failed:", e);
      view.innerHTML = "";
      view.innerHTML = `<div class="empty"><div class="empty-title">页面加载失败</div><div class="empty-sub">${escapeHtmlText(String(e))}</div></div>`;
    }
  },
};

function _loadingPlaceholder() {
  const d = document.createElement("div");
  d.className = "empty";
  d.innerHTML = `<span class="spinner"></span><div class="empty-sub">加载中</div>`;
  return d;
}

function escapeHtmlText(s) {
  return String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

window.addEventListener("hashchange", () => router.resolve());
