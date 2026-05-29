@echo off
chcp 65001 >nul
echo ====================================
echo 停止 SREwise 服务
echo ====================================
echo.

REM 停止 FastAPI 服务
echo [1/7] 停止 FastAPI 服务...
taskkill /FI "WINDOWTITLE eq SuperBizAgent API*" /F >nul 2>&1
if errorlevel 1 (
    echo [信息] FastAPI 服务未运行或已停止
) else (
    echo [成功] FastAPI 服务已停止
)
echo.

REM 停止 CLS MCP 服务
echo [2/7] 停止 CLS MCP 服务...
taskkill /FI "WINDOWTITLE eq CLS MCP Server*" /F >nul 2>&1
if errorlevel 1 (
    echo [信息] CLS MCP 服务未运行或已停止
) else (
    echo [成功] CLS MCP 服务已停止
)
echo.

REM 停止 Monitor MCP 服务
echo [3/7] 停止 Monitor MCP 服务...
taskkill /FI "WINDOWTITLE eq Monitor MCP Server*" /F >nul 2>&1
if errorlevel 1 (
    echo [信息] Monitor MCP 服务未运行或已停止
) else (
    echo [成功] Monitor MCP 服务已停止
)
echo.

REM 停止 K8s MCP 服务 (SREwise)
echo [4/7] 停止 K8s MCP 服务...
taskkill /FI "WINDOWTITLE eq K8s MCP Server*" /F >nul 2>&1
if errorlevel 1 (
    echo [信息] K8s MCP 服务未运行或已停止
) else (
    echo [成功] K8s MCP 服务已停止
)
echo.

REM 停止 Alertmanager MCP 服务 (SREwise)
echo [5/7] 停止 Alertmanager MCP 服务...
taskkill /FI "WINDOWTITLE eq Alertmanager MCP Server*" /F >nul 2>&1
if errorlevel 1 (
    echo [信息] Alertmanager MCP 服务未运行或已停止
) else (
    echo [成功] Alertmanager MCP 服务已停止
)
echo.

REM 停止 Grafana MCP 服务 (SREwise)
echo [6/7] 停止 Grafana MCP 服务...
taskkill /FI "WINDOWTITLE eq Grafana MCP Server*" /F >nul 2>&1
if errorlevel 1 (
    echo [信息] Grafana MCP 服务未运行或已停止
) else (
    echo [成功] Grafana MCP 服务已停止
)
echo.

REM 停止 Docker 容器
echo [7/7] 停止 Milvus 容器...
docker ps --format "{{.Names}}" | findstr "milvus" >nul 2>&1
if not errorlevel 1 (
    docker compose -f vector-database.yml down
    if errorlevel 1 (
        echo [错误] Docker 容器停止失败
    ) else (
        echo [成功] Milvus 容器已停止
    )
) else (
    echo [信息] Milvus 容器未运行
)
echo.

echo ====================================
echo 所有服务已停止！
echo ====================================
echo.
echo 提示:
echo   - 如需完全清理 Docker 数据卷，运行:
echo     docker compose -f vector-database.yml down -v
echo.
pause
