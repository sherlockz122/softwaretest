@echo off
chcp 65001 >nul
cd /d "%~dp0"
title DefectGuard 日志

docker compose --profile full logs -f --tail=100
pause
