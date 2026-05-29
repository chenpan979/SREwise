"""SRE 诊断历史档案 (持久化)

设计目标
========
诊断流程在浏览器/Incidents 页跑完后,前端是 ephemeral 的——刷新或切页就丢。
本模块把每次跑完的完整 session 落盘 (JSONL append-only),让用户能在
"故障档案"页随时回看 / 下载 Markdown 报告。

存储约定
========
- 路径: ``./data/sre_history.jsonl`` (项目根目录, gitignore 之)
- 每行一个 JSON 对象, key 见 ``HistoryRecord`` 字段
- 进程内同时维护 LRU 内存索引 (最近 500 条), 避免每次都全文件扫
- 写入用 ``aiofiles`` async,失败仅 warn,不阻塞主流程

API 字段
========
- ``session_id``       同 LangGraph thread_id
- ``finished_at``      ISO8601 字符串
- ``alert``            诊断入口告警 (可能为 None,如手动 query)
- ``query``            用户自定义 query (可能为 None)
- ``diagnosis``        ``{root_cause, root_cause_category, confidence, ...}``
- ``proposed_actions`` Remediator 提议的动作列表
- ``approved_actions`` HITL 通过后实际批准的动作
- ``execution_results`` Executor 的执行结果列表
- ``report``           Reporter 生成的 Markdown 复盘报告
- ``routing_history``  Supervisor 路由历史 (debug 用)
- ``error``            如果整个流程异常,记下错误摘要
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _PROJECT_ROOT / "data"
_HISTORY_FILE = _DATA_DIR / "sre_history.jsonl"

_MEM_CAP = 500
_MEM: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()  # session_id -> record
_LOADED = False
_LOCK = asyncio.Lock()


def _ensure_dir() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(f"[history] 创建目录失败 {_DATA_DIR}: {e}")


def _load_from_disk() -> None:
    """启动时把磁盘上最近 _MEM_CAP 条加载到内存 (尾部读取)"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    if not _HISTORY_FILE.exists():
        return
    try:
        # 全量行级读取 (历史文件量级不大), 末尾 _MEM_CAP 行入内存
        with _HISTORY_FILE.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        for raw in lines[-_MEM_CAP:]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                sid = rec.get("session_id")
                if sid:
                    _MEM[sid] = rec
            except Exception:
                continue
        logger.info(f"[history] 加载 {_HISTORY_FILE.name} → {len(_MEM)} 条历史")
    except Exception as e:
        logger.warning(f"[history] 加载历史文件失败: {e}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


async def record(
    session_id: str,
    *,
    alert: Optional[Dict[str, Any]] = None,
    query: Optional[str] = None,
    diagnosis: Optional[Dict[str, Any]] = None,
    proposed_actions: Optional[List[Dict[str, Any]]] = None,
    approved_actions: Optional[List[Dict[str, Any]]] = None,
    execution_results: Optional[List[Dict[str, Any]]] = None,
    report: Optional[str] = None,
    routing_history: Optional[List[Any]] = None,
    error: Optional[str] = None,
    status: str = "completed",
) -> None:
    """把一次完整 session 落盘 + 写内存索引。

    多次同 session_id 的 record (例如 interrupt 后 resume 完再次 complete)
    会覆盖内存条目,JSONL 追加新行 → list 端只露最新。
    """
    _load_from_disk()
    rec = {
        "session_id": session_id,
        "finished_at": _now_iso(),
        "status": status,
        "alert": alert,
        "query": query,
        "diagnosis": diagnosis,
        "proposed_actions": proposed_actions or [],
        "approved_actions": approved_actions or [],
        "execution_results": execution_results or [],
        "report": report,
        "routing_history": routing_history or [],
        "error": error,
    }

    async with _LOCK:
        # 内存索引: 同 sid 覆盖, 超过容量从最旧出队
        if session_id in _MEM:
            del _MEM[session_id]
        _MEM[session_id] = rec
        while len(_MEM) > _MEM_CAP:
            _MEM.popitem(last=False)

        # 异步写文件 (单进程下普通 sync write 也够, 避免引入 aiofiles)
        try:
            _ensure_dir()
            line = json.dumps(rec, ensure_ascii=False, default=str)
            await asyncio.to_thread(_append_line, line)
        except Exception as e:
            logger.warning(f"[history] 写入失败 {session_id}: {e}")


def _append_line(line: str) -> None:
    with _HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def list_records(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """返回最近的 N 条 (按 finished_at 倒序)。仅返回摘要字段,避免响应过大。"""
    _load_from_disk()
    # OrderedDict 内默认按插入顺序,因此倒序遍历即按"最新优先"
    all_recs = list(_MEM.values())[::-1]
    page = all_recs[offset: offset + limit]
    return [_to_summary(r) for r in page]


def _to_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    diag = rec.get("diagnosis") or {}
    proposed = rec.get("proposed_actions") or []
    approved = rec.get("approved_actions") or []
    executions = rec.get("execution_results") or []

    # 从 approved_actions[i]._approval 里提取 reviewer 元数据 (human_review 节点写入)
    reviewers, comments = [], []
    for a in approved:
        meta = (a or {}).get("_approval") or {}
        rv = meta.get("reviewer")
        if rv and rv not in reviewers:
            reviewers.append(rv)
        cm = meta.get("comment")
        if cm and cm not in comments:
            comments.append(cm)

    # 推断决策结果 (approve / reject / partial / no_review)
    if not proposed:
        decision = "no_actions"
    elif not approved:
        decision = "rejected"
    elif len(approved) < len(proposed):
        decision = "partial"
    else:
        decision = "approved"

    return {
        "session_id": rec.get("session_id"),
        "finished_at": rec.get("finished_at"),
        "status": rec.get("status"),
        "root_cause": (diag.get("root_cause") or "")[:160],
        "root_cause_category": diag.get("root_cause_category"),
        "confidence": diag.get("confidence"),
        "alert_name": (rec.get("alert") or {}).get("name"),
        "service": (rec.get("alert") or {}).get("service") or diag.get("affected_services"),
        "proposed_count": len(proposed),
        "approved_count": len(approved),
        "executed_ok": sum(1 for e in executions if e.get("ok") or e.get("success")),
        "executed_total": len(executions),
        "has_report": bool(rec.get("report")),
        "error": rec.get("error"),
        # —— 审计字段 (供 Dashboard"人工处置记录"卡 + History 页溯源用) ——
        "decision": decision,             # approved / partial / rejected / no_actions
        "reviewers": reviewers,           # ["console-user", ...]
        "review_comments": comments,      # 备注(去重)
    }


def total_records() -> int:
    _load_from_disk()
    return len(_MEM)


def get_record(session_id: str) -> Optional[Dict[str, Any]]:
    _load_from_disk()
    return _MEM.get(session_id)


def get_report_markdown(session_id: str) -> Optional[str]:
    rec = get_record(session_id)
    if not rec:
        return None
    return rec.get("report") or _fallback_report(rec)


def _fallback_report(rec: Dict[str, Any]) -> str:
    """若原 report 为空,基于其它字段拼一个最小可用 Markdown,确保下载不空文件。"""
    diag = rec.get("diagnosis") or {}
    lines = [
        f"# 故障档案 · {rec.get('session_id')}",
        "",
        f"- **完成时间**: {rec.get('finished_at')}",
        f"- **状态**: {rec.get('status')}",
    ]
    if rec.get("alert"):
        lines += [f"- **告警**: `{json.dumps(rec['alert'], ensure_ascii=False)}`"]
    if rec.get("query"):
        lines += [f"- **用户 query**: {rec['query']}"]
    if diag:
        lines += [
            "",
            "## 根因",
            f"- 类别: `{diag.get('root_cause_category')}`",
            f"- 置信度: `{diag.get('confidence')}`",
            "",
            diag.get("root_cause") or "_无根因摘要_",
        ]
    if rec.get("execution_results"):
        lines += ["", "## 执行结果"]
        for i, ex in enumerate(rec["execution_results"], 1):
            ok = ex.get("ok") or ex.get("success")
            lines += [f"{i}. {'✅' if ok else '❌'} `{ex.get('tool_name', '?')}` "
                      f"args=`{json.dumps(ex.get('args', {}), ensure_ascii=False)}`"]
    if rec.get("error"):
        lines += ["", "## 错误", f"```\n{rec['error']}\n```"]
    return "\n".join(lines)
