@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DefectGuard 演示环境

echo 停止全部演示服务（数据卷保留，下次启动数据还在）...
docker compose --profile full down
if errorlevel 1 (
    echo 停止失败，请查看上方报错。
) else (
    echo 已停止。
)
pause
