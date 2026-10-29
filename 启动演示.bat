@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title DefectGuard 演示环境

echo.
echo ============================================
echo   DefectGuard 演示环境启动
echo ============================================
echo.

rem ---------- 1. Docker 就绪检查 ----------
docker info >nul 2>nul
if not errorlevel 1 goto docker_ok
echo [1/4] Docker 未运行，尝试拉起 Docker Desktop（最长等 120 秒）...
if exist "D:\develop\DockerDesktop\Docker Desktop.exe" (
    start "" "D:\develop\DockerDesktop\Docker Desktop.exe"
) else if exist "C:\Program Files\Docker\Docker\Docker Desktop.exe" (
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
) else (
    echo       未找到 Docker Desktop，请手动启动 Docker 后等待本脚本继续...
)
set waited=0
:wait_docker
timeout /t 5 /nobreak >nul
set /a waited+=5
docker info >nul 2>nul
if not errorlevel 1 goto docker_ok
if %waited% lss 120 goto wait_docker
echo [失败] Docker 120 秒内未就绪。请确认 Docker Desktop 已启动后重试。
pause
exit /b 1

:docker_ok
echo [1/4] Docker 已就绪。

rem ---------- 2. 构建并启动全套服务 ----------
echo [2/4] 构建并启动 mysql / redis / api / worker / scheduler / web（首次构建较慢）...
docker compose --profile full up -d --build
if errorlevel 1 (
    echo.
    echo [失败] 服务启动失败。常见原因：
    echo        - 端口 13306 / 16379 / 18000 / 18080 被占用
    echo        - .env 中的密码或密钥未填写
    echo       请查看上方报错后重试。
    pause
    exit /b 1
)

rem ---------- 3. 等待健康检查 ----------
echo [3/4] 等待服务通过健康检查（最长 5 分钟）...
set waited=0
:wait_ready
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:18000/api/v1/health/ready 2>nul | findstr /r "^200$" >nul
if not errorlevel 1 goto ready_ok
timeout /t 5 /nobreak >nul
set /a waited+=5
if %waited% lss 300 goto wait_ready
echo [失败] 5 分钟内 API 未就绪，双击「查看日志.bat」排查。
pause
exit /b 1

:ready_ok
echo [3/4] API 健康检查通过。

rem ---------- 4. 打开演示页面 ----------
timeout /t 3 /nobreak >nul
echo [4/4] 打开演示页面 http://127.0.0.1:18080
start "" http://127.0.0.1:18080

echo.
echo 演示地址：  http://127.0.0.1:18080
echo API 地址：  http://127.0.0.1:18000
echo.
echo 提示：
echo   - 全新数据库卷需要先建登录账号（.env 里的 DG_BOOTSTRAP_USERNAME）：
echo       docker compose --profile maintenance run --rm bootstrap
echo   - 停止演示：双击「停止演示.bat」（数据卷保留，下次启动数据还在）
echo.
pause
