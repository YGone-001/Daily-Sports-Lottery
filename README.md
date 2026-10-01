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
├── requirements-dev.txt           # 开发/测试依赖（pytest）
├── pytest.ini
├── .gitignore
├── data/                          # ⚠️ 运行时数据，不提交（可由抓取器重建）
│   ├── daily_matches.json         #   每日赛事（抓取器写入）
│   ├── team_strength.json         #   动态球队实力库（Elo）
│   ├── calibrated.json            #   已用于 Elo 校准的比赛 ID
│   ├── prediction_snapshots.json  #   赛前预测快照（不可变历史记录）
│   ├── odds_snapshots.json        #   赛前赔率历史（时序观察记录）
│   ├── settlements.json           #   结算记录（预测快照 × 最终赛果）
│   └── evaluation_rows.json       #   评估样本（预测快照 × 结算）
├── models/
│   ├── poisson_model.py           # 泊松 + Dixon-Coles 比分矩阵
│   ├── predictor.py               # 预测主入口（足球）+ 赔率融合 + EV/Kelly
│   ├── basketball_model.py        # 篮球正态得分模型
│   └── strategy.py                # 价值投注扫描 + 串关组合
├── utils/
│   ├── fetcher_500.py             # 500.com 抓取适配器（4 个数据源）
│   ├── scraper.py                 # 抓取调度：多源合并 + Elo 校准 + 落盘
│   ├── daily_loader.py            # 统一数据层 + 比赛日逻辑 + 查询
│   ├── team_strength.py           # 动态 Elo 实力库
│   ├── atomic_json.py             # 原子 JSON 落盘（临时文件 + fsync + replace）
│   ├── prediction_snapshots.py    # 赛前快照：身份、时序门禁、幂等、不可变
│   ├── odds_snapshots.py          # 赛前赔率历史：规范化、指纹、连续重复抑制
│   ├── settlements.py             # 结算：快照×赛果关联、结果指纹、冲突保护
│   ├── evaluation_rows.py         # 评估样本：快照×结算拼接、溯源校验、源指纹
│   ├── classification_evaluation.py  # 分类评估：accuracy / Brier / LogLoss（纯计算，不落盘）
│   ├── calibration_evaluation.py  # 校准诊断：分箱可靠性 / ECE（纯计算，不落盘）
│   ├── dixon_coles_fitting.py     # 离线 rho 拟合：时间加权 Dixon-Coles 诊断（纯计算，不落盘）
│   ├── dixon_coles_walkforward.py # 走查验证：无泄漏的时序外样本 rho 检验（纯计算，不落盘）
│   ├── dixon_coles_counterfactual.py # 反事实 W/D/L：三策略 Brier / LogLoss 验证（纯计算，不落盘）
│   └── market_coverage.py         # 市场准入：可用盘口覆盖判定（唯一权威定义）
├── tests/                         # pytest 测试（快照行为 / 抓取器集成 / 只读 API）
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

**方式 B：手动**

先在任意终端创建虚拟环境：

```powershell
cd C:\path\to\worldCup
python -m venv .venv
```

然后**按你实际使用的终端**，选下面其中一种。

#### PowerShell

> ⚠️ **PowerShell 默认禁止运行 `.ps1` 脚本**（执行策略为 `Restricted`），直接激活会报：
>
> ```text
> .\.venv\Scripts\Activate.ps1 : 无法加载文件 ...\Activate.ps1，因为在此系统上禁止运行脚本。
> ```
>
> 这**不是本项目的问题**——任何 Python 项目在默认设置的 Windows 上用 PowerShell
> 激活虚拟环境都会遇到同样的拦截。下面两种做法任选其一即可。

**做法 ①：不激活，直接调用虚拟环境里的 `python`（最省事）**

`python.exe` 是程序而非脚本，不受执行策略限制：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m utils.scraper
.\.venv\Scripts\python.exe app.py
```

**做法 ②：先放行执行策略，再正常激活**

```powershell
# 仅对当前窗口生效，关掉窗口即自动恢复（推荐，不改系统设置）
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 或者：仅对当前用户永久放行。本地脚本可运行，从网上下载的脚本仍需签名
# Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m utils.scraper
python app.py
```

#### CMD

CMD 不受 PowerShell 执行策略限制，直接激活即可：

```cmd
.\.venv\Scripts\activate.bat
pip install -r requirements.txt
python -m utils.scraper
python app.py
```

> 也可以直接运行方式 A 的 `start.bat`：它内部直接调用 `.venv\Scripts\python.exe`，
> 完全跳过「激活」这一步，因此在 PowerShell 与 CMD 下都不会被拦截。

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
| GET | `/api/match/<id>/snapshots` | 该场已固化的赛前预测快照（只读，无记录返回空列表） |
| GET | `/api/match/<id>/odds-history` | 该场已捕获的赛前赔率历史，最早在前（只读，无记录返回空列表） |
| GET | `/api/match/<id>/settlements` | 该场结算记录（只读，无记录返回空列表） |
| GET | `/api/dates` | 可用日期与联赛列表 |
| GET | `/api/strategy?sport=` | 策略推荐（价值盘口 + 串关） |
| GET/POST | `/api/refresh` | 手动触发一次抓取 |
| GET | `/api/status` | 系统状态（元信息、日期、球队库、当日统计） |

## 市场覆盖准入

本应用是**体育盘口预测系统**，跟踪的是**有真实盘口覆盖**的比赛，
而不是全球记分板上的每一场。准入判定发生在**数据入口 / canonical 集合边界**，
不是模板层的装饰性过滤。

- **判定依据是市场证据，不是名气**：小联赛只要真有完整盘口就是相关赛事；
  知名联赛若当前集成的数据源给不出可用盘口，则尚未成为被跟踪的市场事件。
  **没有**联赛白名单 / 黑名单 / 球队热度规则。
- **完整盘口才算覆盖**：
  - 足球：完整 1X2（`home_win` + `draw` + `away_win`），或完整大小球
    （`total_line` + `over` + `under`，兼容 `over_2_5` + `under_2_5`）。
  - 篮球：完整胜负（`home_win` + `away_win`），或完整大小分
    （`total_line` + `over` + `under`）。
- **价格校验**：价格必须是数值型（bool 不算）、有限（非 NaN / Infinity）且 **> 1.0**。
  仅有 `matchnum` / `jczq_no` / `total_line` / `handicap_line`，或只有单边、
  或空字典 / None 填充，都**不构成**覆盖。不做字符串到价格的静默转换。
- **准入标记**：被准入的比赛会带上 `market_tracked = true`，
  含义是「该比赛因观察到有效盘口覆盖而被纳入应用」。不会写入 `market_tracked = false`。
- **一旦准入即持续跟踪**：已跟踪比赛后续即使盘口源临时缺供（`odds = None`），
  仍保留在 canonical 集合中、保持 `market_tracked = true`、保留最后已知盘口，
  并继续接受状态 / 比分更新，直到完赛、结算与评估。
- **新比赛必须有覆盖**：首次观察到的比赛若没有可用盘口，则不进入 canonical 日常集合，
  也不会产生任何下游历史（预测快照 / 赔率历史 / 结算 / 评估样本）。
- **历史集合迁移**：既有 `daily_matches.json` 中无标记且无可用盘口的历史行会在下一次
  正常刷新时被移出 canonical 集合；无标记但自带可用盘口的历史行会被升级为
  `market_tracked = true` 并保留。**历史不可变存储**
  （`prediction_snapshots.json` / `odds_snapshots.json` / `settlements.json` /
  `evaluation_rows.json`）一律不受影响，不做追溯性清理。
- **赔率源与 live 源对账**：广域即时比分源仍照常抓取（开赛时间 / 状态 / 比分），
  但广域可见性不再等于准入。赔率行先按竞彩编号（缺失时按 日期 + 主队）挂载到 live 行；
  未挂载但自带可用盘口的赔率行会作为**兜底候选**保留，不会因为一次匹配失败就丢弃有效市场事件。
  挂载成功的事件不会再产生第二条 00:00 兜底记录。
- **当前数据源不变**：仍然只使用仓库已集成的 500.com 派生数据源，未新增任何外部盘口提供方。
  准入判定保持通用，未来接入其他赔率源时同一判据依然适用。

## 赛前预测快照

应用页面的预测仍然是**实时重算**的；快照是一条**独立、不可变**的历史记录，用于
保留「开赛前模型此刻相信什么」，为后续无泄漏的结算与回测提供依据。

- **自动捕获**：由后台抓取线程在每次 `refresh` 中自动固化，**不依赖用户打开任何页面或 API**
  （`/`、`/match/<id>`、`/api/today`、`/api/matches`、`/api/match/<id>` 都不需要被访问）。
  页面/API 路由同样会调用同一套幂等逻辑，但历史记录的存续不再依赖它们。
- **捕获顺序**：抓取 → **市场准入（只保留有盘口覆盖的比赛）** → 合并 canonical 比赛
  → 记录赔率历史 → **用本轮新完赛结果校准 Elo**
  → **捕获预测快照** → **结算已完赛比赛** → **物化评估样本** → 落盘 `daily_matches.json`。
  预测快照在 Elo 校准**之后**生成，因此使用的是当前刷新时刻已知的最新实力。
- **资格**：只有**未开赛**（`upcoming`）的比赛才会获得新快照；
  进行中（`live`）与已完赛（`finished`）不会新建，但**已有快照始终可读**。
- **唯一性**：每场比赛 × 每个模型版本仅一条 canonical 赛前快照。
  它是该比赛/模型版本的**首个**赛前预测记录，不是时间序列，也不代表「收盘预测」。
- **不可变性**：一旦写入，后续的盘口变化、Elo 更新、页面访问、抓取刷新或比赛开赛/完赛，
  都不会改写既有快照。
- **无需盘口**：未开赛比赛即使当时没有盘口也会生成快照，此时市场区块为空；不会虚构赔率。
- **身份**：`snapshot_id = sha256(match_id | slot | model_version)`，确定性且可复现。
- **存储**：`data/prediction_snapshots.json`（运行时生成，已被 `.gitignore` 忽略，文件缺失时惰性创建）。
- **落盘**：原子写入（临时文件 + `fsync` + `os.replace`），并以进程内锁保护并发读改写；
  抓取线程与 HTTP 路由并发时仍只会落盘一条 canonical 快照。
- **模型版本**：足球与篮球**各自独立**的当前版本，由 `config.MODEL_VERSIONS` 提供
  （默认两者均为 `baseline-1`），随每条快照一同落盘。详见「运动专属模型版本」。
- **抓取统计**：`/api/refresh` 返回的 `prediction_snapshots_added` 表示该次刷新**实际新增**的
  快照条数（既有快照、live/finished 比赛、失败的预测都不计入）。

快照字段涵盖比赛标识、开赛时间、生成时间、模型名称与版本、双方 Elo、
模型概率、展示概率、期望值（EV/Kelly）、市场赔率与隐含概率，以及按运动类型保留的比分/得分数据
（足球 `expected_goals` / `top_scores` / `goals_prediction`；篮球 `expected_home_points` /
`expected_away_points` / `expected_total` / `spread` / `over_line` / `over_prob` / `under_prob`）。

## 运动专属模型版本

足球与篮球拥有**各自独立**的当前模型版本，权威配置在 `config.py`：

```python
MODEL_VERSIONS = {
    "football": "football-ad-1",
    "basketball": "baseline-1",
}

MODEL_VERSION = "baseline-1"   # 仅作无运动上下文时的兼容回退
```

- **足球已提升到 `football-ad-1`**：因为足球的攻防输入发生了实质变化
  （见「足球攻防独立化」）；篮球模型未改动，因此**保持 `baseline-1`**。
- **按运动独立提升版本**：只改足球模型时只需提升 `football` 的版本，篮球保持不动，反之亦然。
  这样不会把未改动的那个运动的预测错误地归入新版本，避免污染按
  `(sport, model_name, model_version)` 分组的快照 / 结算 / 评估样本 /
  Accuracy / Brier / LogLoss / ECE 比较。
- **解析优先级**：显式传入的 `model_version` > 该运动当前配置的版本 > 全局兼容回退
  `config.MODEL_VERSION`（仅在没有运动上下文时使用，且不臆造版本号）。
  显式版本始终优先，历史版本 / 测试 / 多版本对比不受配置影响。
- **快照身份不变**：`snapshot_id = sha256(match_id | prematch | model_version)` 未改动，
  也没有把运动加入哈希；因此历史 ID 依然可复现。
- **版本提升后的行为**：仍处于未开赛的比赛，若其当前版本已变化，
  可在保留旧版本快照不变的前提下新增一条新版本快照
  （同一场比赛 × 不同模型版本各自一条 canonical 快照，这是为将来受控的模型对比准备的）；
  已开赛（`live` / `finished`）的比赛不会被追溯补建新版本快照——赛前时间门禁仍然权威。
- **历史 `baseline-1` 快照不可变**：它们仍是权威的历史基线预测，用于与新版本对比，
  不会被改写或迁移。
- **下游自动跟随**：结算、评估样本、分类评估、校准诊断都沿用快照中记录的
  `model_name` / `model_version`，因此新版本会自然成为独立分组，无需改动任何公式。
- **版本号是人工维护的显式标识**：不从 Git SHA / 文件哈希 / 时间戳等自动推断。

## 足球攻防独立化

足球的 `attack_rating` 与 `defense_rating` 现在是**真正独立的两个维度**：

- **Elo 只提供整体实力先验**（`elo_to_attack` / `elo_to_defense`）。
- **历史场均进球**提供攻击证据；**历史场均失球**提供防守证据，两者互不依赖。
  因此「强攻弱守」与「弱攻强守」可以在同一 Elo 下被区分表达。
- **小样本向先验收缩**：`evidence_weight = N / (N + football_strength_prior_matches)`，
  `N` 为累计场次。新球队（`N = 0`）尚无自身进失球证据，此时两个维度等于 Elo 先验（允许相等）。
- **评分尺度不变**：仍为 `0.25 ~ 0.95`，`0.50` 近似中性；
  进攻越强 `attack_rating` 越高，防守越强 `defense_rating` 越高。
  既有 `expected_goals(attack, defense_opponent, base_rate)` 契约保持兼容。
- **既有档案懒升级**：历史 `team_strength.json` 中由旧的「Elo 单一映射」生成的足球档案，
  会在被 `get_team_profile(...)` 读取时按新公式从既有 Elo 与累计进失球重算并落盘；
  只改攻防两个字段，`elo_rating` / 战绩 / 进失球累计等一律不动，重复读取幂等。
- **结果更新顺序**：先更新 Elo → 再累计战绩与进失球 → **然后**由更新后的累计证据推导攻防，
  因此本轮比赛会被反映到新维度里。
- **未改动的部分**：泊松比分矩阵、Dixon-Coles `rho`、`max_goals`、赔率融合、EV / Kelly
  全部保持原样；篮球行为与 `MODEL_VERSIONS["basketball"]` 均未变化。
- **不做拟合**：这两个参数是显式的基础常量，尚未从历史数据拟合，也不做时间衰减 /
  对手强度加权 / 主客场拆分——后续再单独处理。

## 赛前赔率历史

`daily_matches.json` 中每场比赛只保留**最新**盘口（`match["odds"]` 会被新值覆盖）。
赔率历史是一份**附加的**时序层，用于保留盘口随时间的每一次变动。

- **捕获方式**：由后台抓取线程在每次 `refresh` 中**自动捕获**，不依赖用户打开页面。
  流程为：抓取盘口 → 合并到 canonical 比赛记录 → 以该记录的比赛 `id` 捕获变化的赛前盘口 → 落盘。
- **去重规则**：仅抑制**连续重复**——只有与该场「最新一条」规范化盘口相同才跳过。
  因此 `A → B → A` 会保留 3 条，市场来回波动可被完整重建。
- **时序门禁**：只记录**赛前**盘口。未开赛（`upcoming`）允许写入；
  进行中（`live`）与已完赛（`finished`）拒绝新写入，但既有历史始终可读。
  不会把滚球盘口混入赛前历史。
- **规范化与指纹**：先剔除空值（`None` / 空字符串）并按稳定键序排列，再用
  `sha256(json.dumps(..., sort_keys=True))` 生成指纹，与字典顺序无关。
- **不可变**：每次观察写入独立记录，历史记录不会被后续盘口覆盖。
- **存储**：`data/odds_snapshots.json`（运行时生成，已被 `.gitignore` 忽略，文件缺失时惰性创建）。
- **字段**：`snapshot_id`、`match_id`、`sport`、`league`、`home_team`、`away_team`、
  `match_date`、`match_time`、`kickoff_at`、`captured_at`、`source`、`jczq_no`、
  `odds`、`odds_fingerprint`。`odds` 只保留数据源实际提供的字段
  （足球含 `draw`，篮球没有则不写入），不虚构缺失值。
- **抓取统计**：`/api/refresh` 返回的 `odds_snapshots_added` 表示该次刷新**实际新增**的
  历史观察条数；盘口未变的一次刷新为 `0`。

## 结算

结算把一条**已存在的、不可变的赛前预测快照**与其**最终观测到的比赛结果**关联起来。
它只回答「这条历史预测对应的最终结果是什么」，**不计算任何模型表现指标**
（命中率 / Brier / LogLoss / ROI / CLV / 校准误差），也不是货币派彩结算。

- **自动结算**：由后台抓取线程在每次 `refresh` 中自动完成，
  顺序为「… → 捕获预测快照 → **结算已完赛比赛** → 落盘」。
- **前提是快照已存在**：只有**开赛前确实存在**预测快照的比赛才会被结算。
  完结比赛若没有历史预测快照则跳过，**绝不**回溯生成预测或伪造历史快照。
- **资格**：仅 `finished` 且 `score.ft` 为完整双方比分时可结算；
  `upcoming` / `live`、缺少比分、半场比分、非数值比分一律不结算。
- **时间戳校验**：`prediction_generated_at <= kickoff_at` 才被视为有效的赛前证据。
- **唯一性**：一条预测快照至多一条 canonical 结算。
  结算身份只由 `snapshot_id` 决定（不含比分），因此上游结果变化会被识别为**冲突**，
  而不会悄悄产生第二条结算。
- **不可变性**：同一结果重复观测只返回既有记录（不重写 `settled_at` / 比分 / 结果指纹）。
- **结果冲突保护**：上游若对同一快照给出不同最终比分，记录告警（含比赛 ID、快照 ID、
  既有比分、新到比分）并保持既有结算不变；不自动判断哪个比分正确。
- **存储**：`data/settlements.json`（运行时生成，已被 `.gitignore` 忽略，文件缺失时惰性创建，
  原子写入 + 进程内锁保护）。
- **抓取统计**：`/api/refresh` 返回的 `settlements_added` 表示该次刷新**实际新增**的结算条数
  （既有结算、无快照的完赛比赛、live/upcoming、无效比分与冲突都不计入）。

## 评估样本

评估样本把两份**已存在的、不可变的**历史记录拼成一行确定性数据：

```text
预测快照（开赛前模型实际预测了什么）
        +
结算（最终实际发生了什么）
        ↓
评估样本行
```

它是后续模型表现计算的输入数据集，**本层只做数据装配，不计算任何指标**。

- **只消费历史记录**：全部字段逐字复制自权威历史记录——预测类字段来自快照，
  结果类字段来自结算。**不读当前球队实力、不读当前盘口、不读当前比赛状态、
  不重跑模型**（不调用 `predict_match` / `enrich_match`）。
- **不重建历史**：只有「一条预测快照 + 该快照对应的结算」同时存在才生成评估样本；
  缺失任一方一律跳过。完赛但没有历史预测的比赛不会产生评估样本。
- **自动物化**：由后台抓取线程在每次 `refresh` 中完成，
  顺序为「… → 结算已完赛比赛 → **物化评估样本** → 落盘」。
- **回填**：不限于本轮新建的结算。既有结算若缺少评估样本（例如本层上线前就已存在），
  下一次正常刷新会补齐。
- **唯一性**：一条 (快照, 结算) 组合至多一条评估样本；
  `evaluation_id = sha256("evaluation|" + snapshot_id + "|" + settlement_id)`。
- **溯源校验**：物化前校验结算与快照确实指向同一条历史预测
  （`snapshot_id`、`match_id`、`model_name`、`model_version`、
  `prediction_generated_at`、`kickoff_at` 必须一致），
  并要求 `prediction_generated_at <= kickoff_at`。任一项不符则拒绝物化，不静默归一化。
- **源指纹与完整性保护**：`source_fingerprint` 由该行源内容规范化后取 SHA-256。
  同一 `evaluation_id` 且同一指纹 = 正常幂等重放（返回既有样本）；
  同一 `evaluation_id` 但指纹变化 = 源内容完整性冲突，既有样本保持不变。
- **不可变性**：重放不会改写 `materialized_at`、概率、Elo、盘口、比分或结果。
- **不含结论与指标**：不写入 `predicted_outcome` / `correct` / `hit` /
  `accuracy` / `Brier` / `LogLoss` / `ROI` / `CLV` 等任何字段。
- **存储**：`data/evaluation_rows.json`（运行时生成，已被 `.gitignore` 忽略，
  文件缺失时惰性创建，原子写入 + 进程内锁保护）。
- **抓取统计**：`/api/refresh` 返回的 `evaluation_rows_added` 表示该次刷新**实际新增**的
  评估样本条数（既有样本、无结算的快照、无历史预测的完赛比赛、无效拼接、完整性冲突与失败都不计入）。
- **读取方式**：本层只提供服务层查询函数（`get_evaluation_row` /
  `get_evaluation_row_for_snapshot` / `get_evaluation_rows_for_match` /
  `get_all_evaluation_rows`），暂不暴露 HTTP 接口。

## 分类评估

在不可变评估样本之上计算**纯分类指标**：`sample_count`、`accuracy`、
`brier_score`、`multiclass_log_loss`。

- **只消费评估样本**：输入是 `data/evaluation_rows.json`。
  不读 `daily_matches.json` / `prediction_snapshots.json` / `settlements.json` /
  球队实力 / 当前 Elo / 当前盘口，也不调用 `predict_match` / `enrich_match`。
- **纯计算、不落盘**：指标由既有评估样本确定性推导，**不新增**任何运行时指标文件
  （没有 `classification_metrics.json` / `backtest_results.json` / `model_scores.json`）。
  计算函数接收行数据作为输入，可独立测试；`evaluate_all_classification()` 只是
  「读取 + 计算」的便捷入口。
- **概率来源唯一**：一律使用 `evaluation_row["model_probabilities"]`。
  不使用 `display_probabilities`、`market_implied_probabilities`、`market_odds`、
  `expected_values`，不做任何概率融合。
- **历史量纲是百分点**：历史 schema 中概率为 `0..100` 的整数百分点
  （`53` 表示 `0.53`）。**不做** `[0,1]` 与 `[0,100]` 的自动判别，
  `0.60/0.20/0.20` 这类小数在历史 schema 下属畸形数据，会被显式拒绝。
- **取整重归一化**：独立取整后必需类别总和可能是 `99 / 100 / 101`，
  接受该区间并**重归一化到精确的 1.0**；总和落在区间外（如 `90`）直接拒绝，
  不会静默归一化畸形分布。
- **足球三分类**：类别顺序固定为 `home_win` / `draw` / `away_win`。
- **篮球二分类**：类别顺序固定为 `home_win` / `away_win`。
  模型可能写入的兼容字段 `draw = 0` 会被忽略；若 `draw` 非零，
  说明有实质概率质量会被丢弃，按畸形数据拒绝。篮球事实结果为平局同样拒绝，
  不虚构胜者。
- **分组独立**：按 `(sport, model_name, model_version)` 分别计算，
  **不产生**跨运动的混合指标，也不混合不同模型版本。
- **确定性顺序**：摘要按 `sport` / `model_name` / `model_version` 字典序返回，
  不按指标优劣排序、不做模型排名。
- **空输入**：`build_classification_summaries([])` 返回 `[]`，不伪造零值摘要；
  直接对空分组求值会抛出显式错误。
- **严格校验**：畸形行不会被静默跳过，一律抛出 `ClassificationEvaluationError`
  （reason 如 `duplicate_evaluation_id` / `unsupported_sport` / `missing_probability` /
  `invalid_probability` / `invalid_probability_total` / `invalid_actual_outcome` /
  `nonzero_basketball_draw_probability` / `mixed_group`）。

### 指标定义

```text
sample_count          = 该 (sport, model_name, model_version) 组内有效且唯一的评估样本数
                        （每个 evaluation_id 只计一次；重复 evaluation_id 直接报错）

predicted class       = argmax(归一化后的 model_probabilities)
                        并列时取固定类别顺序中靠前者
accuracy              = 正确数 / sample_count        （返回 0.0..1.0，不是百分数）

brier_score           = mean( sum_k (p_k - y_k)^2 )
                        多分类 Brier，**不**再按类别数除一次

multiclass_log_loss   = mean( -ln(p_actual) )         （自然对数）
                        仅取对数时用 epsilon = 1e-15 裁剪，不修改已存储的分布
```

指标以原始浮点数返回，不做取整、不格式化为字符串、不转百分比；
展示层取整属于后续 UI/API 工作。本层**尚未**通过 UI 或 HTTP API 暴露。

## 概率校准诊断

分类指标回答「预测对不对 / 概率好不好」，校准诊断回答另一个问题：

> 当历史模型给某个结果分配了概率 `p` 时，该结果实际发生了多频繁？

- **只消费评估样本**：输入是 `data/evaluation_rows.json`，走既有查询层。
  不读 `daily_matches.json` / `prediction_snapshots.json` / `settlements.json` /
  当前 Elo / 当前盘口，也不调用 `predict_match` / `enrich_match`。
- **只评估 `model_probabilities`**：不使用 `display_probabilities` /
  `market_implied_probabilities` / `market_odds` / `expected_values`，不做概率融合。
- **纯诊断、不落盘、不改概率**：不生成 `calibration.json` 之类的文件，
  不修改任何评估样本，**不产出校准后的概率**（无 Platt / temperature / isotonic 等重映射）。
- **类别空间分开**：足球 `home_win` / `draw` / `away_win` 三分类；
  篮球 `home_win` / `away_win` 二分类，兼容字段 `draw = 0` 不会成为第三个校准类别。
- **按类别 one-vs-rest**：每条评估样本对每个必需类别贡献一个
  `(预测概率 p_k, 观测结果 y_k)` 二元观测；校准按类别分别计算。
- **固定 10 个等宽分箱**：`[0.0,0.1) … [0.8,0.9) [0.9,1.0]`，
  分箱方式 `index = min(int(p * 10), 9)`（最后一箱包含 `1.0`）。不做自适应 / 分位数分箱。
- **分组与排序**：按 `(sport, model_name, model_version)` 独立诊断，
  按该键字典序返回；不混合运动或模型版本，不按 ECE 排名。
- **校验复用**：概率值 / 概率总和 / 篮球 draw / 实际结果的校验与
  分类评估完全一致（同一套 `ClassificationEvaluationError`），
  因此畸形历史行不会被静默丢弃或给出第二套解释。
- **本层尚未**通过 UI 或 HTTP API 暴露；`utils/scraper.py` 未接入校准诊断。

### 诊断定义

每个分箱 `b`：

```text
count                     = 落入该箱的观测数
mean_predicted_probability = 箱内预测概率均值（置信度）
observed_frequency         = 箱内观测结果均值（实际发生频率）
calibration_gap            = |mean_predicted_probability - observed_frequency|
```

空箱的上述三项均为 `None`（不使用 NaN）。

```text
ECE_class = Σ_b (n_b / N) * |mean_probability_b - observed_frequency_b|
```

其中 `N` 为该组评估样本数，`n_b` 为该类别在箱 `b` 的观测数；空箱贡献 0。
因为每条有效样本都会为每个必需类别提供一个概率，所以每个类别都有 `Σ_b n_b == N`。

```text
macro_expected_calibration_error = mean(ECE_class across required classes)
```

足球除以 3，篮球除以 2。这只是诊断性汇总，**不用于排名或择优**。

诊断结果以原始浮点数返回（例如 `0.06327`，而不是 `"6.33%"`），不做取整、不格式化。

## Dixon-Coles rho 拟合（离线诊断）

在不可变评估样本之上做**离线**拟合，回答：

> 给定历史足球模型在开赛前**实际产出**的期望进球，哪个 Dixon-Coles 低比分相关系数
> `rho` 能让时间加权的历史负对数似然最小？

- **只消费评估样本**：使用 `expected_score_data.expected_goals.home/away`（历史 λ / μ）、
  `final_score.home/away`、`kickoff_at`、`sport` / `model_name` / `model_version`。
  **不重建历史 λ**（不调用 `predict_match` / `expected_goals` / `enrich_match`），
  不读当前 Elo / 当前攻防评分 / 当前盘口。
- **仅足球**：篮球分组会被上层构建器直接跳过；对篮球行直接调用分组拟合会显式报错。
- **按模型版本分组**：`(sport, model_name, model_version)` 各自独立拟合，
  绝不跨版本混合（不同版本的 λ 不同，需要各自的诊断）。
- **低比分修正与线上模型完全一致**：
  `0-0 → 1 - λμρ`、`0-1 → 1 + λρ`、`1-0 → 1 + μρ`、`1-1 → 1 - ρ`、其他 `→ 1`。
- **似然只在实际比分上求值**：`P = Poisson(x|λ) · Poisson(y|μ) · tau`，
  拟合期间不对完整比分矩阵做重归一化。
- **时间加权**：默认半衰期 `180` 天，`weight = exp(-ln2 · age_days / 180)`。
  基准时间取**该分组内最晚的 `kickoff_at`**，不使用当前时钟——因此同一批历史数据
  无论哪天拟合结果都完全相同。
- **拟合目标**：加权负对数似然之和 `Σ_i weight_i · -ln(P_i)`（**不**按样本数取平均）。
- **候选网格**：默认 `[-0.25, 0.25]`、步长 `0.005`（整数索引构造，避免浮点漂移）。
  某个候选只要在任一观测上使低比分修正 `<= 0` 或非有限，就**整体剔除**，不做部分拟合、不裁剪。
- **确定性并列裁决**：加权 NLL 最小 → `|rho|` 更小 → 数值更小。
- **诊断输出**：`sample_count`、`low_score_sample_count`、`reference_kickoff_at`、
  `half_life_days`、`effective_sample_weight`、`rho_grid`（含候选数与有效候选数）、
  `fitted_rho`、`weighted_nll`、`rho_zero_weighted_nll`、
  `weighted_nll_improvement_vs_zero`。其中 `rho = 0` 即独立泊松基线。
- **不落盘、不部署**：不生成任何拟合结果文件，不修改 `config.py`，
  **不把拟合结果应用到线上预测**。
- **线上模型不变**：`config.MODEL_CONFIG["dixon_coles_rho"]` 仍为 `-0.15`，
  足球版本仍为 `football-ad-1`，篮球仍为 `baseline-1`。
  如果运行期自动重拟合，同一个 `model_version` 的含义会随时间漂移，破坏既有的
  版本化历史对比体系——因此拟合与部署必须分离，后续由独立的部署任务决定是否采用。

## Dixon-Coles 走查验证（无泄漏时序检验）

上面的拟合是**样本内**工具；它不能作为部署依据。本层补上无泄漏的时序检验，回答：

> 如果 `rho` 只用「目标比赛开赛**之前**已经完赛」的比赛拟合，
> 它给未来比赛打出的分数似然，与当前固定 `rho`、以及独立泊松相比如何？

- **严格过去训练**：对开赛时间 `T` 的目标，训练集严格为 `kickoff_at < T`
  （**严格小于**，不是 `<=`）。同一时刻开赛的多场比赛构成一个**目标桶**，
  彼此不互相训练；桶内所有行共用同一个「仅由更早行」拟合出的 `rho`。
- **扩张窗口**：每个桶的训练集 = 所有严格更早的行（不丢弃旧数据、不用固定滚动窗口）。
  训练拟合仍沿用 `180` 天半衰期，由既有拟合器控制近期影响力。
- **目标权重恒为 1.0**：时间衰减只属于训练拟合，未来测试观测不打折。
- **三个对照**：每条目标观测都用「走查拟合 rho / 当前线上固定 rho / `rho = 0`」分别打分。
  固定对照默认取 `config.MODEL_CONFIG["dixon_coles_rho"]`（当前 `-0.15`），
  也可显式传参覆盖（仅测试用，不改配置）。
- **预热**：`min_train_rows`（默认 `20`）以下的桶不拟合、不打分，计入 `warmup_skipped_count`。
  这是透明的预热规则，不是置信度评级。预热后无可用目标的组返回
  `evaluation_count = 0`、`target_bucket_count = 0`，聚合指标为 `None`（不是 NaN）。
- **分组**：按 `(sport, model_name, model_version)` 独立验证；训练数据绝不跨版本。
  仅支持足球，篮球不产生走查摘要。
- **完整性**：分组之前先对**全部**输入行做 `evaluation_id` 全局重复检测，
  同一 ID 即使篡改 `model_version` 也无法逃避检测；同一组内混用 aware / naive
  时间戳会显式报 `inconsistent_kickoff_timezone`。
- **输出**：每个目标桶的 `rho_path` 条目（`target_kickoff_at` / `train_count` /
  `test_count` / `training_reference_kickoff_at` / `fitted_rho` /
  三项 `*_test_nll`），以及聚合的样本数、预热跳过数、评估数、桶数、
  三项总 / 平均 NLL 与 `nll_improvement_vs_fixed` / `nll_improvement_vs_zero`。
- **只做分数似然诊断**：不计算反事实的胜平负 / Brier / 分类 LogLoss / Accuracy / ECE，
  不选生产 rho、不做排名、不落盘、不接入 refresh。
- **线上模型不变**：`dixon_coles_rho` 仍为 `-0.15`，足球仍为 `football-ad-1`，
  篮球仍为 `baseline-1`。

## Dixon-Coles 反事实 W/D/L 验证

把上面的走查验证从「比分似然」扩展到「比赛结果概率质量」：对每场合格目标比赛，
用**开赛前冻结的历史期望进球**重建胜平负概率，再用 **多分类 Brier** 与
**多分类 LogLoss** 与真实结果比较。

- **概率重建路径**（不重写模型数学）：
  `历史 λ_home / λ_away` → `models.poisson_model.score_matrix(λh, λa, rho=rho)`
  → `win_draw_loss(...)` → 未四舍五入的 `(home_win, draw, away_win)`。
  `max_goals`、泊松 PMF、矩阵归一化、Dixon-Coles tau 全部沿用既有实现，未做任何改动。
- **只比较 rho**：三种策略之间 λ、比分矩阵实现、比分上限、归一化、W/D/L 聚合、
  实际结果完全一致 —— 因此隔离出的正是 Dixon-Coles 的 rho。
  三种策略：**走查拟合 rho**（仅用严格更早比赛拟合）、**当前固定 rho**（默认 `-0.15`，
  可显式覆盖，不改配置）、**rho = 0**（独立泊松）。
- **不使用市场概率**：不读 `market_odds` / `market_implied_probabilities` /
  `display_probabilities` / `expected_values`，也不做市场融合。
- **不使用已存的四舍五入概率**：不从 `evaluation_row["model_probabilities"]` 反推替代
  rho 的概率——那是整数百分点，只对应历史模型当时实际使用的那个 rho。
- **时序完全复用**：合格目标集合、rho 路径、预热计数、版本隔离、同刻分桶、
  严格 `kickoff < T`、全局重复 ID 检测全部来自同一套走查时序
  （`build_walk_forward_plan` / `build_walk_forward_fits`），不存在第二套时序实现。
- **实际结果一致性**：由比分推导 `home_win / draw / away_win`；若行内 `actual_outcome`
  与比分冲突则显式报错，不静默偏向任一字段。
- **指标定义**：Brier = `Σ_k (p_k - y_k)^2`（**不**再按类别数除一次）；
  LogLoss = `-ln(p_actual)`（自然对数，仅在对数求值时用 `1e-15` 裁剪，与分类评估同一常数）。
  **不含 Accuracy、不含 ECE**。
- **输出**：每个合格桶的三策略 Brier / LogLoss 合计与 rho 路径；以及聚合的
  样本数、预热跳过数、评估数、桶数、三策略总/平均 Brier 与 LogLoss，
  以及 `brier_improvement_vs_*` / `log_loss_improvement_vs_*`（正 = 走查序列损失更低，
  仅为描述性差值）。无合格目标时所有 total / mean / improvement 均为 `None`（不是 NaN）。
- **不落盘、不排名、不部署**：不生成任何结果文件，不选生产 rho、不给评级、
  不接入 refresh、不改 `models/predictor.py`。
- **线上模型不变**：`dixon_coles_rho` 仍为 `-0.15`，足球仍为 `football-ad-1`，
  篮球仍为 `baseline-1`。

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
- 足球攻防：`football_strength_prior_matches`（先验收缩等效样本量，默认 5.0）、
  `football_goal_signal_scale`（场均进/失球偏离基准时的评分灵敏度，默认 0.20）
- 篮球：`basketball_base_total`、`basketball_home_advantage`、`basketball_elo_scale`、`basketball_score_std`
- 资金：`kelly_fraction`（默认 1/4 Kelly）

## 模型说明

**足球**：Elo 提供整体实力先验，攻防评分由历史场均进/失球**独立**推导
（见「足球攻防独立化」）→ Elo 差 → 双方期望进球 λ → 泊松比分矩阵（含 Dixon-Coles 低比分修正 ρ=−0.15）
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
| 日志临时 | `*.log`、`*.tmp`、`*.bak`、`*.corrupt` | 运行时产物 |
| 编辑器/系统 | `.idea/`、`.vscode/`、`.DS_Store` | 个人环境差异 |
| AI 工作区 | `.workbuddy-ai/` | 本地助手记忆，与项目运行无关 |
| 历史备份 | `_legacy_backup/` | 改造前旧文件 |

克隆仓库后，`data/` 为空是正常的 —— 首次启动会自动抓取填充。

## 测试

```bash
pip install -r requirements-dev.txt   # 安装 pytest
python -m compileall .
pytest -q
```

测试在临时目录中运行，不会读写真实的 `data/`。

## License

MIT License，详见 `LICENSE`。

## 注意事项

- 数据来自公开网页抓取，若 500.com 改版可能导致解析失效，需更新 `utils/fetcher_500.py`。
- 仅供研究与娱乐，**不构成任何投注建议**，请理性对待。
