import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 从 .env 读取环境变量（纯 Python 实现，无额外依赖）
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DATA_DIR = os.path.join(BASE_DIR, "data")

# Flask
DEBUG = os.environ.get("DEBUG", "true").lower() == "true"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))

# ---------------------------------------------------------------------------
# 数据源配置（每日体彩预测）
# ---------------------------------------------------------------------------
# 500.com 数据源
FETCH_500_LIVE = os.environ.get("FETCH_500_LIVE", "true").lower() == "true"
FETCH_500_TIMEOUT = float(os.environ.get("FETCH_500_TIMEOUT", "20"))

# 竞彩 playid
PLAYID_FOOTBALL = "269"   # 竞彩足球
PLAYID_BASKETBALL = "275"  # 竞彩篮球

# 后台抓取间隔（秒）。默认 30 分钟，覆盖竞彩开售时段
SCRAPE_INTERVAL_SECONDS = int(os.environ.get("SCRAPE_INTERVAL_SECONDS", "1800"))

# 数据缓存
DATA_CACHE_TTL_SECONDS = int(os.environ.get("DATA_CACHE_TTL_SECONDS", "300"))

# 逻辑比赛日切割点（北京时间）。凌晨 6 点前的比赛归前一天
MATCHDAY_CUTOFF_HOUR = int(os.environ.get("MATCHDAY_CUTOFF_HOUR", "6"))

# ---------------------------------------------------------------------------
# 模型参数
# ---------------------------------------------------------------------------
MODEL_CONFIG = {
    # ---- 足球 ----
    "base_goals_per_match": 1.35,     # 场均进球基准
    "home_advantage_elo": 60.0,       # 主场优势（Elo 加分）
    "elo_scale": 400.0,               # Elo 换算尺度
    "attack_sensitivity": 0.35,       # Elo 差对进球的敏感度
    "away_attack_sensitivity": 0.30,  # 客队敏感度略低
    "max_lambda": 4.0,                # 单队期望进球上限
    "min_lambda": 0.35,               # 下限
    "dixon_coles_rho": -0.15,         # Dixon-Coles 低比分修正
    "odds_weight": float(os.environ.get("ODDS_WEIGHT", "0.20")),  # 赔率融合权重
    "value_threshold": 0.05,          # 正 EV 判定阈值 (5%)

    # ---- 篮球 ----
    "basketball_base_total": 215.0,   # NBA/CBA 场均总分基准
    "basketball_base_total_cba": 205.0,
    "basketball_home_advantage": 2.5,  # 主场让分
    "basketball_elo_scale": 200.0,    # 篮球 Elo 尺度
    "basketball_score_std": 11.5,     # 单队得分标准差
    "basketball_pace": 1.0,           # 节奏系数

    # ---- 资金管理 ----
    "kelly_fraction": 0.25,           # 1/4 Kelly
    "monte_carlo_runs": 10000,
}

# 联赛强度系数（用于未知球队的 Elo 兜底估算）
LEAGUE_STRENGTH = {
    "英超": 1900, "西甲": 1880, "意甲": 1850, "德甲": 1860, "法甲": 1820,
    "欧冠": 1920, "欧联": 1830, "欧国联": 1800,
    "英冠": 1720, "德乙": 1700, "西乙": 1680, "意乙": 1680, "法乙": 1660,
    "中超": 1620, "中甲": 1550, "日职": 1700, "日乙": 1620, "韩K联": 1660,
    "美职": 1650, "巴西甲": 1700, "阿甲": 1680, "荷甲": 1750, "葡超": 1760,
    "比甲": 1700, "土超": 1690, "俄超": 1680, "苏超": 1680,
    "NBA": 1900, "CBA": 1700, "欧冠篮球": 1850, "欧篮联": 1820,
}
DEFAULT_LEAGUE_ELO = 1600
