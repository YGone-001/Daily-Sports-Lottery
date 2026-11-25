# 每日运营：赛程、赛前快照与赛后赛果

本文给出**可重复使用**的每日操作序列。默认全部只读；真实网络刷新、真实外部证据采集与真实生产写入是三个独立授权。

## 1. 日常链路

```text
1. 获取竞彩赛程与盘口（只读启动 / 显式刷新）
2. 捕获符合条件的不可变赛前预测
3. 查看 pending 赛果
4. 选择有既有提供商绑定的 NBA 目标
5. 生成有界请求计划
6. 获准后采集官方 + BDL 赛果证据
7. 离线校验 + 既有严格批量预览
8. 单独批准后执行结算
9. 读取评估摘要与剩余缺口
```

## 2. 命令序列

### 2.1 启动与赛前数据

默认只读：

```powershell
.\start.bat
```

需要本机写能力时（本次进程有效，不是一次性生产批准）：

```powershell
.\start.bat --refresh
```

也可手动执行一次刷新（会写入受保护存储，需操作者授权）：

```powershell
.\.venv\Scripts\python.exe -m utils.scraper
```

### 2.2 查看 pending 赛果

```powershell
.\.venv\Scripts\python.exe -m tools.basketball_result_delivery --data-dir data `
  --output reports\pending.json --markdown reports\pending.md
```

该命令只读，不联网、不写库。

### 2.3 选择 NBA 目标（离线）

```powershell
.\.venv\Scripts\python.exe -m tools.nba_result_workflow select --data-dir data --max-targets 3
```

只选择「有待结算赛前快照 + 既有唯一 BDL 绑定 + 开球已过」的 NBA 赛事。
输出包含：

- `targets`：本次可进入证据采集的目标；
- `not_selected_eligible`：合格但超出 `--max-targets` 的目标；
- `rejected`：无绑定、非 NBA、已结算、缺快照、身份冲突等及精确原因；
- `queue`：按 `PROVIDER_BOUND_AND_PENDING` / `PROVIDER_IDENTITY_NOT_ESTABLISHED` /
  `ALREADY_SETTLED` / `IDENTITY_CONFLICT` / `RESULT_CONFLICT` /
  `MISSING_OR_INVALID_PREGAME_SNAPSHOT` 分类的只读计数。

显式指定目标：

```powershell
.\.venv\Scripts\python.exe -m tools.nba_result_workflow select --data-dir data `
  --target-match-id 500j-2026-11-16-7312 --target-match-id 500j-2026-11-16-7313
```

### 2.4 生成有界请求计划（离线）

```powershell
.\.venv\Scripts\python.exe -m tools.nba_result_workflow plan --data-dir data `
  --allow-network --max-requests 7 --max-cost 0
```

`plan` 只计算请求数与端点，不联网。默认 `max_requests=0` 时状态为
`NETWORK_AUTHORIZATION_REQUIRED`；预算不足时为 `REQUEST_BUDGET_INSUFFICIENT`。

### 2.5 采集真实赛果证据（需明确网络授权）

```powershell
.\.venv\Scripts\python.exe -m tools.nba_result_workflow acquire --data-dir data `
  --allow-network --max-requests 7 --max-cost 0 --pacing-seconds 12.1
```

- 请求预算：`1 次官方赛程发现 + 每场 1 次官方页 + 每场 1 次 BDL`；可用
  `--official-game-id MATCH=GAMEID` 跳过发现页。
- BDL 频率：起始间隔至少 12.1 秒；官方页至少 1 秒。
- 无自动重试；不访问付费端点；不订阅；不注册；`--max-cost 0`。
- 凭据只从环境变量 `DSL_BDL_API_KEY` 或本地 `.DSL_BDL_API_KEY` 读取，不写入证据。
- 原始响应保存到 `reports/result-evidence/nba-official/` 与
  `reports/result-evidence/balldontlie/`，并写 `manifest.json`。
- 已验证目标可被缓存复用；重复运行命中缓存时请求数为 0。

### 2.6 严格批量预览（离线）

```powershell
.\.venv\Scripts\python.exe -m tools.nba_result_workflow preview --data-dir data
```

复用既有 `tools.nba_confirmed_result.preview_batch()`，输出：

```text
accepted target set
snapshot ids + digests
official / BDL game ids
verified final scores
settlement / evaluation ids
current generation
seven-store hashes
preview_sha256
write scope
```

被拒绝的目标不会静默进入 accepted 集；预览不授权执行。

### 2.7 执行（需单独生产授权）

只有操作者批准精确预览后，才使用既有入口：

```powershell
$env:DSL_RUNTIME_WRITES_ENABLED = "true"
.\.venv\Scripts\python.exe -m tools.nba_confirmed_result `
  --targets-file reports\result-evidence\preview-targets.json `
  --execute --expected-preview-sha256 <preview_sha256> `
  --receipt reports\result-evidence\execution-receipt.json
```

只写 `daily_matches.json`、`settlements.json`、`evaluation_rows.json`；
其余四库必须字节不变。一次执行 = 一次锁段 = 一次代号递增 = 一份回执；
不自动重试、不回滚、不做广域刷新或 Canonical 对账。

### 2.8 读取评估与覆盖

```powershell
.\.venv\Scripts\python.exe -m utils.evidence_status
curl.exe http://127.0.0.1:5000/api/evaluation-summary
curl.exe http://127.0.0.1:5000/api/status
```

## 3. 证据与缓存

默认证据目录：`reports/result-evidence/`（Git 本地排除）。

```text
reports/result-evidence/
├── manifest.json                 # 最近一次运行的目标、请求与验证结果
├── runs/<run_id>.json            # 每次运行的完整清单
├── preview-targets.json          # 可交给 tools.nba_confirmed_result 的目标
├── preview.json                  # 严格预览
├── nba-official/
│   ├── nba-discovery-<date>.response
│   └── nba-game-<gameId>.response
└── balldontlie/
    └── bdl-<gameId>.response
```

每个请求记录：provider、endpoint、`started_at`、`observed_at`、HTTP 状态、
响应字节数、SHA-256、目标 match id、provider game id、代码 SHA。
**不记录凭据。**

## 4. 边界

- **足球**：`wanchang` 是足球完场源；支持 `e=YYYY-MM-DD` 日期参数，但历史抓取需单独授权。
  缺失比分不补造；历史盘口方向仍为 `PARTIAL`。
- **非 NBA 篮球**：当前没有获准的竞赛兼容赛果源，保持 `SOURCE_NOT_AVAILABLE`；
  不得把 NBA 官方/BDL 工具用于 WNBA、德篮甲、欧篮联、西篮甲、巴西 LDB 等赛事。
- **身份**：联赛编号、队名相似、同日时间接近都不是独立身份证据；不得自动归并
  `500j` / `500lq` 或转移 provider id。
- **评估**：3 场结算不等于 3 次正确预测；小样本不用于声明模型优越性。
- **授权**：`--allow-network`、`--execute`、`DSL_RUNTIME_WRITES_ENABLED=true`
  都是软件门，不替代操作者批准。
