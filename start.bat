@echo off
REM ============================================================
REM  每日实时体彩预测系统 - 一键启动
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo ============================================================
echo   每日实时体彩预测系统
echo ============================================================
echo.

REM ---- 1. 定位 Python ----
set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
) else (
    where python >nul 2>nul && set "PY=python"
)

if not defined PY (
    echo [错误] 未找到 Python, 请先安装 Python 3.10+
    pause
    exit /b 1
)
echo [1/3] 使用 Python: %PY%

REM ---- 2. 检查依赖 ----
%PY% -c "import flask, requests" 2>nul
if errorlevel 1 (
    echo [2/3] 缺少依赖, 正在安装...
    %PY% -m pip install -r requirements.txt
) else (
    echo [2/3] 依赖已就绪
)

REM ---- 3. 首次运行抓取数据 ----
if not exist "data\daily_matches.json" (
    echo [3/3] 首次运行, 抓取赛事数据...
    %PY% -m utils.scraper
) else (
    echo [3/3] 数据已存在, 跳过首次抓取
)

echo.
echo 正在启动服务 http://127.0.0.1:5000
echo 按 Ctrl+C 停止
echo.
%PY% app.py
