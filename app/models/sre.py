"""SREwise API 请求 / 响应模型。"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SREDiagnoseRequest(BaseModel):
    """SREwise 多 Agent 诊断请求。

    三种触发方式:
    1. 仅 session_id  → 自动拉取当前 critical 告警
    2. 提供 alert     → 基于该告警诊断
    3. 提供 query     → 用户主动发起 (例如 "检查 data-sync-service 是否健康")
    """

    session_id: Optional[str] = Field(default="default", description="会话 ID")
    alert: Optional[Dict[str, Any]] = Field(default=None,
                                            description="触发告警(可选)")
    query: Optional[str] = Field(default=None,
                                 description="用户主动诊断的描述(可选)")
    auto_fetch_alert: bool = Field(default=True,
                                   description="alert/query 都为空时是否自动拉告警")

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "demo-001",
                "auto_fetch_alert": True,
            }
        }


class SREApproveRequest(BaseModel):
    """HITL 审批请求 (Step 3)。

    审批粒度: 整批审批 (selected_indices 留空 = 全批准)。
    """

    session_id: str = Field(description="待审批的 session_id (= 中断时的 thread_id)")
    approve: bool = Field(description="true=批准, false=拒绝整批")
    selected_indices: Optional[List[int]] = Field(
        default=None,
        description="部分批准时选中的动作索引列表 (与 proposed_actions 对齐)",
    )
    comment: Optional[str] = Field(default=None, description="审批人备注")
    reviewer: Optional[str] = Field(default="anonymous", description="审批人 ID")

    class Config:
        json_schema_extra = {
            "example": {
                "session_id": "demo-001",
                "approve": True,
                "selected_indices": [0],
                "comment": "确认是 v42 内存问题,执行优先级 1 的回滚",
                "reviewer": "alice",
            }
        }
