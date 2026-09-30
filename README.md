# 每日实时体彩预测终端 (Daily Sports-Lottery Terminal)

一个基于 Flask 的**每日竞彩预测系统**。从 500.com 自动抓取中国体彩竞彩赛程与赔率，
覆盖**竞彩足球 + 竞彩篮球**，用 Elo 强度 + 泊松/Dixon-Coles（足球）与正态得分分布（篮球）
模型输出胜平负概率、最可能比分、大小球倾向、悬念指数，并结合 **EV / Fractional Kelly**
扫描正期望值（价值）盘口与最优串关组合。

> 说明：本项目输出的是模型视角下的概率估计与模拟结果，不代表确定预测，也不构成投注建议。

## 主要功能

- **每日自动抓取**：后台守护线程每 30 分钟从 500.com 拉取竞彩赛程与赔率，热更新本地数据。
- **足篮双模型**：
  - 足球 —— Elo → 期望进球 → **泊松 + Dixon-Coles 低比分修正** → 胜平负 / 比分 / 大小球。
  - 篮球 —— Elo → 预期得分 → **正态分布**建模分差与总分 → 胜负 / 让分 / 大小分。
- **市场盘口对撞**：模型概率与庄家赔率隐含概率按权重融合，自动嗅探正 EV 的「🔥 价值」盘口。
- **智能策略舱**：扫描全盘价值漏洞，用组合数学生成「二串一 / 三串一 / 四串一」最优组合。
- **动态凯利仓位计算器**：滑动本金条，用 Fractional Kelly (1/4) 算出最佳注码与预期回报。
- **六维战力雷达**：单场详情页提供基于 Chart.js 的攻防战力多维对比雷达图。
- **历史复盘**：已完赛比赛展示「预测 vs 实际」，统计命中率，滚动校准球队 Elo。
- **动态球队实力库**：任意球队池自动匹配 Elo（联赛锚点 + 排名 + 结果滚动校准），无需外部 API。

## 技术栈

- Python 3.10+
- Flask 3.x
- Requests（HTTP 抓取）
- Jinja2 模板 + Chart.js（雷达图）
- 本地 JSON 数据（`data/*.json`）

## 数据源架构

系统采用**双源互补**策略，全部走 HTTP 直出，**无需浏览器渲染**：

| 数据源 | 地址 | 提供 |
| --- | --- | --- |
| 即时比分（足球） | `live.500.com/index.php` | 赛程、**开赛时间**、竞彩编号、实时比分 |
| 即时比分（篮球） | `live.500.com/lq.php` | 内联 `matchList` 赛程 + 竞彩官方赔率 |
| 竞彩赔率 | `trade.500.com/static/public/{lot}/newxml/pl/pl_{play}_2.xml` | 胜平负 / 让分 / 大小分盘口 |
| 完场比分 | `live.500.com/wanchang.php` | 历史赛果（用于 Elo 校准与复盘） |

两个源通过 **竞彩编号**（如 `3001`）精确合并：赛程取时间与队名，赔率取盘口。

> 曾尝试的 `trade.500.com/jczq` 页面为纯 AJAX，`live.500.com` 的静态 XML 为 2019 年陈旧数据，
> 均已弃用；上面的端点才是当前可用的权威来源。

## 项目结构

```text
worldCup/
├── app.py                         # Flask 入口：页面路由 + JSON API + 后台抓取线程
├── config.py                      # 运行配置、数据源、足球/篮球模型参数
├── start.bat                      # Windows 一键启动脚本
├── start.sh                       # Linux / macOS 一键启动脚本
├── requirements.txt
├── .gitignore
├── data/                          # ⚠️ 运行时数据，不提交（可由抓取器重建）
│   ├── daily_matches.json         #   每日赛事（抓取器写入）
│   ├── team_strength.json         #   动态球队实力库（Elo）
│   └── calibrated.json            #   已用于 Elo 校准的比赛 ID
├── models/
│   ├── poisson_model.py           # 泊松 + Dixon-Coles 比分矩阵
│   ├── predictor.py               # 预测主入口（足球）+ 赔率融合 + EV/Kelly
│   ├── basketball_model.py        # 篮球正态得分模型
│   └── strategy.py                # 价值投注扫描 + 串关组合
├── utils/
│   ├── fetcher_500.py             # 500.com 抓取适配器（4 个数据源）
│   ├── scraper.py                 # 抓取调度：多源合并 + Elo 校准 + 落盘
│   ├── daily_loader.py            # 统一数据层 + 比赛日逻辑 + 查询
│   └── team_strength.py           # 动态 Elo 实力库
├── templates/                     # base / index / match / strategy / history
└── static/
    ├── css/style.css              # 「数据终端 × 金融看板」风格，明暗双主题
    └── js/app.js                  # 进度条动画、手动同步
```

> **关于 `data/`**：目录下所有 `*.json` 都是抓取器生成的可再生数据，
> 已加入 `.gitignore`。`git clone` 后没有这些文件也能启动，
> 执行一次 `python -m utils.scraper` 即可重建（首次启动会自动执行）。


## 启动方式

> 环境要求：**Python 3.10+**，依赖仅 `flask` + `requests`（无需 API Key、无需浏览器）。

### Windows

**方式 A：一键启动（推荐）**

双击项目根目录的 `start.bat`，脚本会自动完成：

1. 定位 Python（优先 `.venv`，其次内置环境，最后 `PATH`）
2. 检查依赖，缺失则自动 `pip install -r requirements.txt`
3. 首次运行自动抓取赛事数据
4. 启动服务并输出访问地址

**方式 B：手动（PowerShell / CMD）**

```powershell
cd C:\path\to\worldCup

# 1) 创建并激活虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # PowerShell
:: .\.venv\Scripts\activate.bat    # CMD

# 2) 安装依赖
pip install -r requirements.txt

# 3) 首次抓取数据
python -m utils.scraper

# 4) 启动服务
python app.py
```

> 若 PowerShell 报「禁止运行脚本」，先执行：
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

### Linux / macOS

**方式 A：一键启动（推荐）**

```bash
cd /path/to/worldCup
chmod +x start.sh      # 首次需赋执行权限
./start.sh
```

脚本会自动：创建 `.venv` → 安装依赖 → 首次抓取数据 → 启动服务。

**方式 B：手动（Bash）**

```bash
cd /path/to/worldCup

# 1) 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装依赖
pip install -r requirements.txt

# 3) 首次抓取数据
python -m utils.scraper

# 4) 启动服务
python app.py
```

**方式 C：不用虚拟环境（快速试用）**

```bash
cd /path/to/worldCup
pip3 install -r requirements.txt
python3 -m utils.scraper
python3 app.py
```

> 部分发行版需先装 venv 模块：
> - Debian/Ubuntu：`sudo apt install python3-venv`
> - CentOS/RHEL：`sudo yum install python3`
> - macOS：`brew install python3`

### 访问

两种系统均访问：

```text
http://127.0.0.1:5000
```

启动后会自动拉起**后台抓取线程**（默认每 30 分钟刷新一次竞彩赛程与赔率）。

### 后台常驻运行（可选）

Linux 下如需长期后台运行：

```bash
# 用 nohup 后台运行并记录日志
nohup python app.py > app.log 2>&1 &

# 停止
pkill -f app.py
```

生产环境建议用 WSGI 服务器：

```bash
pip install gunicorn
gunicorn app:app --bind 0.0.0.0:5000 --workers 1
```

> **注意**：后台抓取线程在多 worker 下会重复启动，建议 `--workers 1`，
> 或改用独立进程 / cron 定时执行 `python -m utils.scraper`，Web 进程只读数据。

### 自定义端口 / 监听地址

Linux / macOS：

```bash
HOST=0.0.0.0 PORT=8000 DEBUG=false SCRAPE_INTERVAL_SECONDS=900 ./start.sh
# 或直接：HOST=0.0.0.0 PORT=8000 DEBUG=false python3 app.py
```

Windows PowerShell：

```powershell
$env:HOST="0.0.0.0"; $env:PORT="8000"; $env:DEBUG="false"; python app.py
```

完整变量列表见下方 [配置说明](#配置说明)。

## 页面入口

| 页面 | 地址 | 说明 |
| --- | --- | --- |
| 今日赛事 | `/` | 按联赛分组的赛事卡片、概率条、价值标签；支持足球/篮球与日期筛选 |
| 比赛详情 | `/match/<match_id>` | 概率卡片、比分预测、赔率表、凯利计算器、六维雷达 |
| 智能策略 | `/strategy` | 价值盘口清单 + 二/三/四串一推荐 + 本金滑条 |
| 历史复盘 | `/history` | 完赛的「预测 vs 实际」对照与命中率 |

## API 接口

| 方法 | 地址 | 说明 |
| --- | --- | --- |
| GET | `/api/today?sport=` | 逻辑比赛日赛事（含预测） |
| GET | `/api/matches?date=&sport=` | 按日期/类别查询赛事 |
| GET | `/api/match/<id>` | 单场详情 |
| GET | `/api/dates` | 可用日期与联赛列表 |
| GET | `/api/strategy?sport=` | 策略推荐（价值盘口 + 串关） |
| GET/POST | `/api/refresh` | 手动触发一次抓取 |
| GET | `/api/status` | 系统状态（元信息、日期、球队库、当日统计） |

## 配置说明

### 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 监听地址（对外暴露用 `0.0.0.0`） |
| `PORT` | `5000` | 服务端口 |
| `DEBUG` | `true` | 调试模式，生产环境设为 `false`（关闭后后台线程单进程启动） |
| `SCRAPE_INTERVAL_SECONDS` | `1800` | 后台抓取间隔（秒） |
| `MATCHDAY_CUTOFF_HOUR` | `6` | 逻辑比赛日切割点（北京时间，凌晨归前一天） |
| `ODDS_WEIGHT` | `0.20` | 赔率隐含概率融合权重 |
| `DATA_CACHE_TTL_SECONDS` | `300` | 数据缓存时间 |
| `FETCH_500_LIVE` | `true` | 是否启用 500.com 抓取 |
| `FETCH_500_TIMEOUT` | `20` | 抓取超时（秒） |

### 模型参数

集中在 `config.py` 的 `MODEL_CONFIG`：

- 足球：`base_goals_per_match`、`home_advantage_elo`、`attack_sensitivity`、`dixon_coles_rho`、`value_threshold`
- 篮球：`basketball_base_total`、`basketball_home_advantage`、`basketball_elo_scale`、`basketball_score_std`
- 资金：`kelly_fraction`（默认 1/4 Kelly）

## 模型说明

**足球**：Elo 差 → 双方期望进球 λ → 泊松比分矩阵（含 Dixon-Coles 低比分修正 ρ=−0.15）
→ 胜平负概率 → 与市场隐含概率按 `ODDS_WEIGHT` 融合 → EV / Kelly。

**篮球**：Elo 差 → 双方预期得分 → 分差正态分布 → 胜负概率；总分用正态分布建模，
盘口总分线（`yszf`）优先于联赛基准。

**价值判断**：`EV = 模型概率 × 赔率 − 1`，`EV > value_threshold (5%)` 判定为价值盘口；
仓位用 Fractional Kelly 计算。

## 部署说明

- WSGI 入口对象为 `app:app`。
- 静态文件 `static/`，模板 `templates/`。
- **多进程部署时后台抓取线程会重复启动**，建议用独立进程跑
  `python -m utils.scraper` 做定时抓取（cron / 计划任务），Web 进程只读数据。
- 生产环境建议 `DEBUG=false` 并置于 gunicorn/uwsgi 之后。
- `data/` 目录需可写（抓取器需要落盘）。

### 版本控制说明

`.gitignore` 已排除以下内容，**无需提交到远端仓库**：

| 类别 | 内容 | 原因 |
| --- | --- | --- |
| Python 产物 | `__pycache__/`、`*.pyc` | 编译缓存，本地生成 |
| 虚拟环境 | `.venv/`、`venv/` | 体积大，可重建 |
| 运行时数据 | `data/*.json` | 抓取器生成，每次刷新覆盖，可自动重建 |
| 密钥配置 | `.env` | 含敏感信息 |
| 日志临时 | `*.log`、`*.tmp`、`*.bak` | 运行时产物 |
| 编辑器/系统 | `.idea/`、`.vscode/`、`.DS_Store` | 个人环境差异 |
| AI 工作区 | `.workbuddy-ai/` | 本地助手记忆，与项目运行无关 |
| 历史备份 | `_legacy_backup/` | 改造前旧文件 |

克隆仓库后，`data/` 为空是正常的 —— 首次启动会自动抓取填充。

## License

MIT License，详见 `LICENSE`。

## 注意事项

- 数据来自公开网页抓取，若 500.com 改版可能导致解析失效，需更新 `utils/fetcher_500.py`。
- 仅供研究与娱乐，**不构成任何投注建议**，请理性对待。
