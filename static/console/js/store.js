/**
 * 极简 pub/sub store。
 *
 * 用途
 * ====
 * - 全局态: health 状态 / 主题 / pending 数 / langfuse host
 * - 跨页面共享只读快照,不需要 Redux
 *
 * 用法
 * ====
 *   store.set("health.status", "ok");
 *   const off = store.on("health.status", (v) => ...);
 */

const _state = {
  "theme": "dark",
  "health": null,                // 后端 /health 完整对象
  "pending.count": 0,
  "langfuse.host": "http://localhost:3000",
};

const _subs = new Map();         // key -> Set<fn>

export const store = {
  get(key) { return _state[key]; },
  set(key, value) {
    const prev = _state[key];
    if (prev === value) return;
    _state[key] = value;
    const subs = _subs.get(key);
    if (subs) subs.forEach((fn) => {
      try { fn(value, prev); } catch (e) { console.error("[store sub]", e); }
    });
  },
  on(key, fn) {
    if (!_subs.has(key)) _subs.set(key, new Set());
    _subs.get(key).add(fn);
    return () => _subs.get(key).delete(fn);
  },
  snapshot() { return { ..._state }; },
};
