@echo off
REM ============================================================
REM  每日实时体彩预测终端 - 一键启动（Windows）
REM ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo ============================================================
echo   每日实时体彩预测终端 (Daily Sports Lottery Terminal)
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
    echo [错误] 未找到 Python，请先安装 Python 3.10 或更高版本
    pause
    exit /b 1
)
echo [1/4] 使用 Python: %PY%

REM ---- 2. 创建虚拟环境与检查 Python 依赖 ----
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] 创建虚拟环境 .venv ...
    "%PY%" -m venv .venv
    if errorlevel 1 goto PYTHON_ERROR
    set "PY=.venv\Scripts\python.exe"
)

"%PY%" -c "import flask, requests" >nul 2>nul
if errorlevel 1 (
    echo [2/4] 正在安装 Python 依赖...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto PYTHON_ERROR
) else (
    echo [2/4] Python 依赖已就绪
)

REM ---- 3. 首次抓取赛事数据 ----
if not exist "data\daily_matches.json" (
    echo [3/4] 首次运行，正在抓取赛事数据...
    "%PY%" -m utils.scraper
    if errorlevel 1 goto PYTHON_ERROR
) else (
    echo [3/4] 赛事数据已就绪
)

REM ---- 4. 构建 React 前端 ----
where npm >nul 2>nul
if errorlevel 1 goto NO_NPM

echo [4/4] 检测到 Node.js / npm 环境
pushd frontend
if not exist "node_modules" (
    echo [前端] 正在根据锁定依赖安装...
    call npm ci
    if errorlevel 1 (
        popd
        goto FRONTEND_ERROR
    )
)

echo [前端] 正在构建前端成品...
call npm run build
if errorlevel 1 (
    popd
    goto FRONTEND_ERROR
)
popd
set "SERVE_FRONTEND=1"
goto START_SERVER

:NO_NPM
echo [4/4] 未检测到 npm，将使用旧模板界面
set "SERVE_FRONTEND="

:START_SERVER
echo.
echo ============================================================
echo   服务启动完成:
if defined SERVE_FRONTEND (
    echo   - Web 界面: http://127.0.0.1:5000
) else (
    echo   - Web 界面: http://127.0.0.1:5000 ^(旧模板^)
)
echo   - API 接口: http://127.0.0.1:5000/api
echo   - 按 Ctrl+C 可停止服务
echo ============================================================
echo.
start "" "http://127.0.0.1:5000"
"%PY%" app.py
if errorlevel 1 goto PYTHON_ERROR
exit /b 0

:FRONTEND_ERROR
echo.
echo [错误] 前端构建失败，服务未启动。请检查上方日志。
pause
exit /b 1

:PYTHON_ERROR
echo.
echo [错误] 后端准备或服务启动失败，请检查上方日志。
pause
exit /b 1
