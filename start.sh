#!/usr/bin/env bash
# ============================================================
#  每日实时体彩预测终端 - 一键启动 (Linux / macOS / Git Bash)
# ============================================================
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  每日实时体彩预测终端 (Daily Sports Lottery Terminal)"
echo "============================================================"
echo

# ---- 定位虚拟环境内的 Python ----
venv_python() {
    if [ -x ".venv/bin/python" ]; then
        echo ".venv/bin/python"
    elif [ -x ".venv/Scripts/python.exe" ]; then
        echo ".venv/Scripts/python.exe"
    fi
}

# ---- 1. 定位系统 Python ----
PY=""
if [ -n "$(venv_python)" ]; then
    PY="$(venv_python)"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
fi

if [ -z "$PY" ]; then
    echo "[错误] 未找到 Python，请先安装 Python 3.10+"
    exit 1
fi
echo "[1/4] 使用 Python: $PY"

# ---- 2. 创建虚拟环境与检查依赖 ----
if [ -z "$(venv_python)" ]; then
    echo "[2/4] 创建虚拟环境 .venv ..."
    "$PY" -m venv .venv
    PY="$(venv_python)"
    if [ -z "$PY" ]; then
        echo "[错误] 虚拟环境创建失败（缺少 venv 模块？）"
        echo "        Debian/Ubuntu: sudo apt install python3-venv"
        exit 1
    fi
fi

if ! "$PY" -c "import flask, requests" >/dev/null 2>&1; then
    echo "[2/4] 安装 Python 依赖..."
    "$PY" -m pip install -q -r requirements.txt
else
    echo "[2/4] Python 依赖已就绪"
fi

# ---- 3. 首次抓取数据 ----
if [ ! -f "data/daily_matches.json" ]; then
    echo "[3/4] 首次运行，抓取赛事数据..."
    "$PY" -m utils.scraper
else
    echo "[3/4] 赛事数据已就绪"
fi

# ---- 4. 构建前端 ----
if command -v npm >/dev/null 2>&1; then
    echo "[4/4] 检测到 Node.js / npm 环境"
    if [ ! -d "frontend/node_modules" ]; then
        if [ -f "frontend/package-lock.json" ]; then
            echo "[前端] 正在根据锁定依赖安装 (npm ci)..."
            (cd frontend && npm ci)
        else
            echo "[前端] 正在安装前端依赖 (npm install)..."
            (cd frontend && npm install)
        fi
    fi
    echo "[前端] 正在构建前端成品..."
    (cd frontend && npm run build)
    export SERVE_FRONTEND=1
else
    echo "[4/4] 未检测到 npm 环境，将使用旧模板界面"
fi

echo
echo "============================================================"
echo "  服务启动完成:"
if [ "${SERVE_FRONTEND:-}" = "1" ]; then
echo "  ► Web 界面: http://127.0.0.1:5000"
else
echo "  ► Web 界面: http://127.0.0.1:5000 (旧模板)"
fi
echo "  ► API 接口: http://127.0.0.1:5000/api"
echo "  ► 按 Ctrl+C 可停止全部服务"
echo "============================================================"
echo

# 启动 Python 后端服务
exec "$PY" app.py
