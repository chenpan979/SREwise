# MCP Servers

> **SREwise 的工具网关层** — 5 个 MCP 服务器提供 21 个工具，覆盖日志查询、监控数据、K8s 操作、告警管理、Grafana 查询等 SRE 全场景。

## 📚 服务列表

### 1. CLS Server (`cls_server.py`)
**日志查询服务** - 端口 8003 | **5 个工具**

提供腾讯云 CLS（日志服务）查询能力，支持日志搜索、模式分析、主题管理。

**核心工具：**
- `get_current_timestamp` - 获取当前时间戳（用于日志查询时间范围）
  - **risk_level**: `read`
- `get_topic_info_by_name` - 查询日志主题信息
  - **risk_level**: `read`
- `search_log` - 通用日志搜索（支持 CLS 查询语法）
  - **risk_level**: `read`
- `search_service_logs` - 服务日志查询（支持级别筛选：error/warn/info）
  - **risk_level**: `read`
- `analyze_log_pattern` - 日志模式分析（提取高频错误模式）
  - **risk_level**: `read`

---

### 2. Monitor Server (`monitor_server.py`)
**监控数据服务** - 端口 8004 | **2 个工具**

提供系统监控指标查询，包括 CPU、内存、磁盘、网络等。

**核心工具：**
- `query_cpu_metrics` - CPU 使用率查询（支持时间范围、聚合间隔）
  - **risk_level**: `read`
  - 参数：`service_name`, `start_time`, `end_time`, `interval`
- `query_memory_metrics` - 内存使用查询（包含使用率、RSS、缓存等）
  - **risk_level**: `read`
  - 参数：`service_name`, `start_time`, `end_time`, `interval`

---

### 3. K8s Server (`k8s_server.py`)
**Kubernetes 操作服务** - 端口 8005 | **8 个工具**

提供 K8s 集群管理能力，包括 Pod 查询、扩缩容、重启、回滚等。

**核心工具：**
- `get_pod_status` - 查询 Pod 状态（运行状态、重启次数、资源使用）
  - **risk_level**: `read`
- `get_pod_logs` - 获取 Pod 日志（支持 tail、时间范围）
  - **risk_level**: `read`
- `describe_pod` - 详细描述 Pod（Events、Conditions、Volumes）
  - **risk_level**: `read`
- `list_pods` - 列出命名空间下所有 Pod
  - **risk_level**: `read`
- `scale_deployment` - 扩缩容 Deployment（修改副本数）
  - **risk_level**: `write`
  - ⚠️ 需要 HITL 审批
- `restart_pod` - 重启 Pod（删除 Pod 触发重建）
  - **risk_level**: `write`
  - ⚠️ 需要 HITL 审批
- `rollback_deployment` - 回滚 Deployment 到上一版本
  - **risk_level**: `destructive`
  - ⚠️ 需要 HITL 审批
- `get_deployment_history` - 查询 Deployment 发布历史
  - **risk_level**: `read`

---

### 4. Alertmanager Server (`alertmanager_server.py`)
**告警管理服务** - 端口 8006 | **4 个工具**

提供 Prometheus Alertmanager 告警查询、静默、确认等操作。

**核心工具：**
- `get_active_alerts` - 获取当前活跃告警（支持按服务、严重级别筛选）
  - **risk_level**: `read`
  - 参数：`service_name`, `severity`, `limit`
- `get_alert_history` - 查询历史告警（支持时间范围、状态筛选）
  - **risk_level**: `read`
- `silence_alert` - 静默告警（临时屏蔽告警通知）
  - **risk_level**: `write`
  - ⚠️ 需要 HITL 审批
  - 参数：`alert_name`, `duration`, `comment`
- `acknowledge_alert` - 确认告警（标记为已知晓）
  - **risk_level**: `write`
  - 参数：`alert_id`, `comment`

---

### 5. Grafana Server (`grafana_server.py`)
**Grafana 查询服务** - 端口 8007 | **2 个工具**

提供 Grafana 仪表盘查询、面板数据获取能力。

**核心工具：**
- `get_dashboard_url` - 获取服务对应的 Grafana 仪表盘 URL
  - **risk_level**: `read`
  - 参数：`service_name`, `time_range`
- `query_panel_data` - 查询 Grafana 面板数据（支持 PromQL 查询）
  - **risk_level**: `read`
  - 参数：`dashboard_id`, `panel_id`, `time_range`

---

## 📊 工具统计

| 服务器 | 端口 | 工具数 | read | write | destructive |
|--------|------|--------|------|-------|-------------|
| CLS Server | 8003 | 5 | 5 | 0 | 0 |
| Monitor Server | 8004 | 2 | 2 | 0 | 0 |
| K8s Server | 8005 | 8 | 4 | 3 | 1 |
| Alertmanager Server | 8006 | 4 | 2 | 2 | 0 |
| Grafana Server | 8007 | 2 | 2 | 0 | 0 |
| **总计** | - | **21** | **15** | **5** | **1** |

---

## 🚀 快速开始

### 环境要求

- **Python**: 3.11+
- **FastMCP**: 最新版本
- **依赖**: `pip install fastmcp`

### 启动服务

**方式一：使用 Windows 批处理（推荐）**
```powershell
# 一键启动所有服务（包含 MCP 服务器）
.\start-windows.bat
```

**方式二：使用 Makefile（Linux/macOS）**
```bash
make start        # 启动所有服务（包含 MCP）
make mcp-start    # 仅启动 MCP 服务
make mcp-stop     # 停止 MCP 服务
make mcp-status   # 查看服务状态
```

**方式三：手动启动单个服务**
```bash
# 启动 CLS 服务
python mcp_servers/cls_server.py

# 启动 Monitor 服务
python mcp_servers/monitor_server.py

# 启动 K8s 服务
python mcp_servers/k8s_server.py

# 启动 Alertmanager 服务
python mcp_servers/alertmanager_server.py

# 启动 Grafana 服务
python mcp_servers/grafana_server.py
```

### 验证服务

```bash
# 检查所有 MCP 服务是否启动
curl http://localhost:8003/health  # CLS
curl http://localhost:8004/health  # Monitor
curl http://localhost:8005/health  # K8s
curl http://localhost:8006/health  # Alertmanager
curl http://localhost:8007/health  # Grafana
```

---

## 💡 使用示例

### 场景 1：OOM 故障诊断

**Agent 自动执行流程：**

```
1. [Historian] 召回历史相似故障
   └─ query_memory_metrics("data-sync-service")  # Monitor Server

2. [Diagnostician] 根因诊断
   ├─ get_pod_status("data-sync-service")        # K8s Server
   ├─ get_pod_logs("data-sync-service")          # K8s Server
   ├─ search_service_logs(level="error")         # CLS Server
   └─ get_active_alerts(service="data-sync")     # Alertmanager Server

3. [Remediator] 生成候选动作
   └─ 提议: scale_deployment(replicas=3)         # K8s Server (write)

4. [Human Review] HITL 审批
   └─ 用户批准 scale_deployment

5. [Executor] 执行修复
   └─ scale_deployment("data-sync-service", 3)   # K8s Server

6. [Reporter] 生成复盘
   └─ 写入 Neo4j KG + 生成 Markdown 报告
```

---

### 场景 2：服务响应慢排查

**工具调用示例：**

```python
# 1. 查询 CPU 趋势
query_cpu_metrics(
    service_name="api-gateway",
    start_time="2024-05-29 10:00:00",
    end_time="2024-05-29 11:00:00",
    interval="1m"
)
# 返回: [{"timestamp": "...", "value": 85.3, "threshold": 80}, ...]

# 2. 查询内存使用
query_memory_metrics(
    service_name="api-gateway",
    start_time="2024-05-29 10:00:00",
    interval="1m"
)
# 返回: [{"timestamp": "...", "usage_percent": 92.1, "rss_mb": 1843}, ...]

# 3. 查看 Pod 状态
get_pod_status(
    pod_name="api-gateway-7d8f9c-abc12",
    namespace="production"
)
# 返回: {"status": "Running", "restarts": 3, "cpu": "850m", "memory": "1.8Gi"}

# 4. 获取错误日志
search_service_logs(
    service_name="api-gateway",
    log_level="error",
    keyword="timeout",
    limit=50
)
# 返回: [{"timestamp": "...", "level": "ERROR", "message": "..."}, ...]

# 5. 查看 Grafana 仪表盘
get_dashboard_url(
    service_name="api-gateway",
    time_range="1h"
)
# 返回: "https://grafana.example.com/d/api-gateway?from=now-1h&to=now"
```

---

### 场景 3：告警静默（维护窗口）

**工具调用示例：**

```python
# 1. 查看当前活跃告警
get_active_alerts(
    service_name="payment-service",
    severity="critical"
)
# 返回: [{"alert_name": "HighMemoryUsage", "severity": "critical", ...}]

# 2. 静默告警（需要 HITL 审批）
silence_alert(
    alert_name="HighMemoryUsage",
    duration="2h",
    comment="计划维护窗口，预期内存升高"
)
# 返回: {"silence_id": "abc123", "expires_at": "2024-05-29 13:00:00"}

# 3. 确认告警
acknowledge_alert(
    alert_id="alert-456",
    comment="已知晓，正在处理"
)
# 返回: {"acknowledged": true, "acknowledged_by": "ops-team"}
```

---

## 🔧 高级配置

### 接入真实 API

当前所有服务器返回 **模拟数据**。接入真实 API 步骤：

#### 1. 腾讯云 CLS（日志服务）

```bash
# 安装 SDK
pip install tencentcloud-sdk-python

# 配置环境变量
export TENCENTCLOUD_SECRET_ID="your-secret-id"
export TENCENTCLOUD_SECRET_KEY="your-secret-key"
export CLS_REGION="ap-guangzhou"
```

在 `cls_server.py` 中集成：
```python
from tencentcloud.cls.v20201016 import cls_client, models
from tencentcloud.common import credential

cred = credential.Credential(
    os.getenv("TENCENTCLOUD_SECRET_ID"),
    os.getenv("TENCENTCLOUD_SECRET_KEY")
)
client = cls_client.ClsClient(cred, os.getenv("CLS_REGION"))
```

#### 2. Kubernetes API

```bash
# 安装 SDK
pip install kubernetes

# 配置 kubeconfig
export KUBECONFIG=~/.kube/config
```

在 `k8s_server.py` 中集成：
```python
from kubernetes import client, config

config.load_kube_config()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
```

#### 3. Prometheus Alertmanager

```bash
# 配置 Alertmanager URL
export ALERTMANAGER_URL="http://alertmanager.example.com:9093"
```

在 `alertmanager_server.py` 中集成：
```python
import requests

def get_active_alerts():
    resp = requests.get(f"{ALERTMANAGER_URL}/api/v2/alerts")
    return resp.json()
```

#### 4. Grafana API

```bash
# 配置 Grafana
export GRAFANA_URL="https://grafana.example.com"
export GRAFANA_API_KEY="your-api-key"
```

在 `grafana_server.py` 中集成：
```python
import requests

headers = {"Authorization": f"Bearer {GRAFANA_API_KEY}"}
resp = requests.get(f"{GRAFANA_URL}/api/dashboards/uid/{uid}", headers=headers)
```

---

### 自定义 Mock 数据

如果暂时无法接入真实 API，可以修改 Mock 数据以模拟实际场景：

**示例：修改 `k8s_server.py` 中的 Pod 状态**

```python
@mcp.tool()
def get_pod_status(pod_name: str, namespace: str = "default") -> dict:
    """查询 Pod 状态
    
    risk_level: read
    """
    # 自定义 Mock 数据
    return {
        "pod_name": pod_name,
        "namespace": namespace,
        "status": "CrashLoopBackOff",  # 模拟异常状态
        "restarts": 15,                 # 模拟频繁重启
        "cpu_usage": "950m",            # 模拟高 CPU
        "memory_usage": "3.8Gi",        # 模拟高内存
        "node": "node-03",
        "age": "2h15m"
    }
```

---

## 🛡️ 安全机制

### Risk Level 分级

所有工具必须在 docstring 末尾标注 `risk_level`：

- **`read`**: 只读操作，无副作用（如查询日志、指标）
  - 不需要 HITL 审批
  - Diagnostician / Historian 可用

- **`write`**: 写操作，有副作用但可逆（如扩缩容、重启）
  - 需要 HITL 审批
  - 仅 Executor 可用

- **`destructive`**: 破坏性操作，不可逆（如回滚、删除）
  - 需要 HITL 审批 + 二次确认
  - 仅 Executor 可用

### 工具过滤机制

`app/agent/sre/tool_filter.py` 会根据 Agent 角色过滤工具：

```python
# Diagnostician 只能拿 read 工具
diagnostician_tools = filter_tools_by_risk(all_tools, allowed=["read"])

# Executor 可以拿 write + destructive 工具
executor_tools = filter_tools_by_risk(all_tools, allowed=["write", "destructive"])
```

---

## 📚 参考资料

- [FastMCP 文档](https://github.com/jlowin/fastmcp)
- [MCP 协议规范](https://modelcontextprotocol.io/)
- [LangGraph 文档](https://langchain-ai.github.io/langgraph/)
- [Kubernetes Python Client](https://github.com/kubernetes-client/python)
- [腾讯云 CLS SDK](https://cloud.tencent.com/document/product/614)
- [Prometheus Alertmanager API](https://prometheus.io/docs/alerting/latest/clients/)
- [Grafana HTTP API](https://grafana.com/docs/grafana/latest/developers/http_api/)
- [主项目 README](../README.md)
- [系统架构文档](../ARCHITECTURE.md)

---

## 🔍 故障排查

### MCP 服务启动失败

**问题**: `Address already in use`

**解决**:
```bash
# Windows
netstat -ano | findstr :8003
taskkill /PID <PID> /F

# Linux/macOS
lsof -ti:8003 | xargs kill -9
```

### Agent 拿不到工具

**问题**: `No tools available for diagnostician`

**排查**:
1. 检查 MCP 服务是否启动：`curl http://localhost:8003/health`
2. 检查工具 docstring 是否有 `risk_level` 标注
3. 检查 `tool_filter.py` 的过滤逻辑

### HITL 审批卡住

**问题**: Eval 跑到 HITL 就停了

**解决**: 
- 检查 `app/eval/runner.py` 的 `approval_policy` 配置
- 手动审批：访问 http://localhost:9900/console/ → 故障诊断 → 待审批

---

**⚠️ 注意**: 当前版本所有 MCP 服务器返回 **模拟数据**，生产环境需按上述步骤接入真实 API。

**🎉 SREwise MCP Servers — 21 个工具，覆盖 SRE 全场景！**
