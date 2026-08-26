@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动本机状态监控...
python server.py
pause
