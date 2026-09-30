#!/usr/bin/env bash
# ============================================================
#  每日实时体彩预测系统 - 一键启动 (Linux / macOS / Git Bash)
# ============================================================
set -e
cd "$(dirname "$0")"

echo "============================================================"
echo "  每日实时体彩预测系统"
echo "============================================================"
echo

# ---- 定位虚拟环境内的 Python（兼容 bin/ 与 Scripts/ 两种布局）----
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

# ---- 2. 创建虚拟环境 ----
if [ -z "$(venv_python)" ]; then
    echo "[2/4] 创建虚拟环境 .venv ..."
    "$PY" -m venv .venv
    PY="$(venv_python)"
    if [ -z "$PY" ]; then
        echo "[错误] 虚拟环境创建失败（缺少 venv 模块？）"
        echo "        Debian/Ubuntu: sudo apt install python3-venv"
        exit 1
    fi
else
    echo "[2/4] 虚拟环境已存在"
fi

# ---- 3. 检查依赖 ----
if ! "$PY" -c "import flask, requests" >/dev/null 2>&1; then
    echo "[3/4] 安装依赖..."
    "$PY" -m pip install -q --upgrade pip
    "$PY" -m pip install -q -r requirements.txt
else
    echo "[3/4] 依赖已就绪"
fi

# ---- 4. 首次抓取数据 + 启动 ----
if [ ! -f "data/daily_matches.json" ]; then
    echo "[4/4] 首次运行，抓取赛事数据..."
    "$PY" -m utils.scraper
else
    echo "[4/4] 数据已存在，跳过首次抓取"
fi

echo
echo "正在启动服务 http://127.0.0.1:5000"
echo "按 Ctrl+C 停止"
echo
exec "$PY" app.py
